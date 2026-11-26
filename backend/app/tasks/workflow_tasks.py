"""工作流 Celery 任务：执行运行、扫描审批超时、回收僵尸运行。

所有数据库操作都通过 ``app.worker.run_in_worker_loop`` 提交到进程内唯一的
常驻事件循环，保证 asyncpg 连接池始终在同一个循环里使用。

任务失败策略：

- ``workflow.execute_run`` 捕获所有异常并记录日志，运行行保持 ``running``，
  交给 stale 扫描重新投递；绝不因为单条任务异常让 worker 进程退出。
- 扫描任务失败同样只记录日志，下一轮 beat 会重试。
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.db import get_session_factory
from app.models.run import WorkflowRun
from app.services.run_service import (
    expire_waiting_approvals,
    find_stale_runs,
)
from app.worker import celery_app, run_in_worker_loop
from app.observability.propagation import TRACE_KWARG, trace_payload_from_current

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ 入队助手


def enqueue_workflow_run(
    run_id: int, resume_node_id: str | None = None
) -> bool:
    """投递一次工作流执行；关闭或 broker 故障时返回 ``False``，绝不抛出。

    关键节点：
        1. Celery 未启用时直接返回 False，调用方保持 pending 由扫描兜底；
        2. 有活动 trace 时把最小上下文放进 ``__trace__``，由 worker 恢复链路；
        3. 入队失败只告警不抛出，API/审批响应不能被 broker 故障拖垮。

    Args:
        run_id: 运行主键。
        resume_node_id: 审批恢复节点；为空表示从 start 开始。

    Returns:
        True 表示已成功投递到 broker。
    """
    if not get_settings().celery_enabled:
        return False
    send_kwargs: dict[str, Any] = {}
    try:
        payload = trace_payload_from_current()
        if payload:
            send_kwargs["kwargs"] = {TRACE_KWARG: payload}
    except Exception:  # noqa: BLE001 - 传播失败不能影响业务入队
        logger.warning("工作流 trace 上下文注入失败（run_id=%s）", run_id, exc_info=True)
    try:
        celery_app.send_task(
            "workflow.execute_run",
            args=[run_id, resume_node_id],
            queue="q.workflow",
            retry=False,
            **send_kwargs,
        )
        return True
    except Exception:  # noqa: BLE001 - broker 故障不能影响 API / 审批响应
        logger.warning("工作流任务投递失败（run_id=%s）", run_id, exc_info=True)
        return False


# --------------------------------------------------------------- 执行单次运行


async def _execute_run(run_id: int, resume_node_id: str | None) -> dict[str, Any]:
    """在 worker 事件循环内推进一次运行；新的会话按运行租户收口。

    关键节点：
        1. worker 没有请求级租户，只能先按 run_id 读运行，再切到运行租户；
        2. finally 必须恢复租户，避免同一 worker 后续任务串租户；
        3. 运行不存在时返回 not_found，不创建任何 trace。
    """
    from app.workflow.engine import WorkflowEngine

    async with get_session_factory()() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return {"run_id": run_id, "status": "not_found"}

        token = set_tenant_id(run.tenant_id)
        try:
            result = await WorkflowEngine(
                session,
                run_id,
                heartbeat_factory=get_session_factory(),
            ).run(resume_node_id=resume_node_id)
        finally:
            reset_tenant_id(token)

    return {
        "run_id": result.run_id,
        "status": result.status,
        "error": result.error,
        "resume_node_id": resume_node_id,
        "waiting_node_id": result.waiting_node_id,
    }


@shared_task(name="workflow.execute_run", ignore_result=True)
def execute_run(
    run_id: int,
    resume_node_id: str | None = None,
    __trace__: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行或恢复一个工作流运行。

    ``__trace__`` 是 Celery 传播用的保留参数，只由 ``task_prerun`` 信号消费；
    这里保留形参是为了让 broker 投递的 kwargs 能被任务接受。
    """
    try:
        return run_in_worker_loop(
            lambda: _execute_run(run_id, resume_node_id)
        )
    except Exception:  # noqa: BLE001 - 保留 running 状态交给 stale 扫描
        logger.exception("工作流任务执行异常（run_id=%s）", run_id)
        return {
            "run_id": run_id,
            "status": "error",
            "resume_node_id": resume_node_id,
        }


# ------------------------------------------------------------- 审批超时扫描


async def _scan_approval_timeouts() -> list[int]:
    async with get_session_factory()() as session:
        return await expire_waiting_approvals(session)


@shared_task(name="workflow.scan_approval_timeouts", ignore_result=True)
def scan_approval_timeouts() -> dict[str, Any]:
    """把超过 expires_at 的等待审批运行收敛为失败。"""
    try:
        expired = run_in_worker_loop(_scan_approval_timeouts)
    except Exception:  # noqa: BLE001 - 扫描失败等下一轮 beat
        logger.exception("审批超时扫描失败")
        return {"expired_run_ids": []}
    return {"expired_run_ids": expired}


# ------------------------------------------------------------- 僵尸运行回收


async def enqueue_stale_runs() -> list[int]:
    """查找心跳过期的 running 运行并重新投递；返回成功入队的 id。"""
    async with get_session_factory()() as session:
        stale_ids = await find_stale_runs(
            session,
            older_than_s=get_settings().workflow_stale_after_s,
        )

    enqueued: list[int] = []
    for run_id in stale_ids:
        if enqueue_workflow_run(run_id):
            enqueued.append(run_id)
    return enqueued


@shared_task(name="workflow.requeue_stale_runs", ignore_result=True)
def requeue_stale_runs() -> dict[str, Any]:
    """周期性回收心跳过期的 running 运行。"""
    try:
        enqueued = run_in_worker_loop(enqueue_stale_runs)
    except Exception:  # noqa: BLE001 - 扫描失败等下一轮 beat
        logger.exception("僵尸运行回收失败")
        return {"enqueued_run_ids": []}
    return {"enqueued_run_ids": enqueued}


__all__ = [
    "enqueue_stale_runs",
    "enqueue_workflow_run",
    "execute_run",
    "requeue_stale_runs",
    "scan_approval_timeouts",
]
