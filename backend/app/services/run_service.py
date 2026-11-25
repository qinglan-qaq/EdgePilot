"""工作流运行业务逻辑：创建 pending 运行、查询、取消与人工审批。

运行创建只落库，不触发执行、不伪造 ``trace_id``：worker 领取后才会写
``running`` / span / 节点结果。创建时按 DSL 节点声明顺序预插全部
``workflow_node_runs``（status=pending, attempt=1），供后续引擎幂等推进。

执行入队是尽力而为：broker 不可用只记告警，绝不让 API 返回 500。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound
from app.core.tenancy import get_tenant_id, reset_tenant_id, set_tenant_id
from app.models.run import WorkflowNodeRun, WorkflowRun
from app.observability import get_tracer
from app.observability.span import TraceHandle
from app.repositories.workflow_repo import WorkflowRepo
from app.services.audit_service import AuditService
from app.workflow.dsl import nodes_of

logger = logging.getLogger(__name__)

NON_TERMINAL_STATUSES = frozenset({"pending", "running", "waiting_approval"})


def enqueue_workflow_run(
    run_id: int, resume_node_id: str | None = None
) -> bool:
    """模块级别名：便于测试 monkeypatch，并延迟导入 Celery 任务模块。"""
    from app.tasks.workflow_tasks import (
        enqueue_workflow_run as _enqueue_workflow_run,
    )

    return _enqueue_workflow_run(run_id, resume_node_id)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _close_run_trace(run: WorkflowRun, status: str, *, name: str = "") -> None:
    """关闭已开启的运行 trace；不存在的 trace_id 或观测故障都静默跳过。"""
    if not run.trace_id:
        return
    try:
        handle = TraceHandle(
            tracer=get_tracer(),
            tenant_id=run.tenant_id,
            trace_id=run.trace_id,
            root_span_id=run.root_span_id or "",
            name=name or f"workflow_run.{run.id}",
            source="workflow_run",
            start_time=run.started_at or datetime.now(timezone.utc),
        )
        handle.set_output({"status": status, "error": run.error})
        handle.close(status)
    except Exception:  # noqa: BLE001 - 观测失败不能影响状态机
        logger.warning("关闭运行 trace 失败（run_id=%s）", run.id, exc_info=True)


@dataclass
class RunContext:
    run: WorkflowRun
    workflow_id: int
    workflow_name: str
    workflow_version: int
    dsl: dict
    nodes: list[WorkflowNodeRun]


@dataclass
class RunListRow:
    run: WorkflowRun
    workflow_id: int
    workflow_name: str
    workflow_version: int


def order_node_runs(
    node_runs: list[WorkflowNodeRun], dsl: dict[str, Any]
) -> list[WorkflowNodeRun]:
    """按 DSL 节点声明顺序排序；DSL 中不存在的节点排末尾并按 node_id 排序。"""
    order: dict[str, int] = {}
    for index, node in enumerate(nodes_of(dsl)):
        node_id = node.get("id")
        if isinstance(node_id, str):
            order.setdefault(node_id, index)
    fallback = len(order) + len(nodes_of(dsl))
    return sorted(
        node_runs,
        key=lambda row: (
            order.get(row.node_id, fallback),
            row.node_id,
        ),
    )


class RunService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WorkflowRepo(session)

    # ----------------------------------------------------------------- create
    async def create(
        self,
        workflow_id: int,
        *,
        input_data: dict | None = None,
        trigger_type: str = "manual",
        trigger_payload: dict | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RunContext:
        tenant_id = get_tenant_id()
        workflow = await self.repo.get_workflow(tenant_id, workflow_id)
        if workflow is None:
            raise NotFound("工作流不存在")
        if workflow.status != "published" or workflow.current_version < 1:
            raise Conflict("工作流尚未发布")

        version = await self.repo.get_version(
            tenant_id, workflow_id, workflow.current_version
        )
        if version is None:
            raise Conflict("工作流已发布版本不存在")

        run = WorkflowRun(
            tenant_id=tenant_id,
            workflow_version_id=version.id,
            trigger_type=trigger_type,
            trigger_payload=trigger_payload,
            status="pending",
            input=input_data,
            trace_id=None,
        )
        self.session.add(run)
        # flush 只为拿到自增 run.id；仍在同一事务内，节点行随后一起提交。
        await self.session.flush()

        for node in nodes_of(version.dsl):
            self.session.add(
                WorkflowNodeRun(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    node_id=node["id"],
                    node_type=node["type"],
                    status="pending",
                    attempt=1,
                )
            )
        await self.session.commit()
        await self.session.refresh(run)

        await AuditService.write(
            self.session,
            action="workflow.run",
            resource_type="workflow",
            resource_id=workflow_id,
            detail={
                "workflow_id": workflow_id,
                "run_id": run.id,
                "version": workflow.current_version,
                "trigger_type": trigger_type,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        # 入队是旁路：broker 故障只能记录告警，运行仍保持 pending 可被重新投递。
        try:
            enqueue_workflow_run(run.id)
        except Exception:  # noqa: BLE001 - 绝不因 broker 失败破坏创建响应
            logger.warning("工作流运行入队失败（run_id=%s）", run.id, exc_info=True)
        return await self.detail(run.id)

    # ------------------------------------------------------------------- read
    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        workflow_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[RunListRow], int]:
        tenant_id = get_tenant_id()
        rows, total = await self.repo.list_runs(
            tenant_id,
            page,
            page_size,
            workflow_id=workflow_id,
            status=status,
        )
        return [
            RunListRow(
                run=run,
                workflow_name=name,
                workflow_id=wf_id,
                workflow_version=version,
            )
            for run, name, wf_id, version in rows
        ], total

    async def detail(self, run_id: int) -> RunContext:
        tenant_id = get_tenant_id()
        run = await self.repo.get_run(tenant_id, run_id)
        if run is None:
            raise NotFound("运行不存在")

        version = await self.repo.get_version_by_id(
            tenant_id, run.workflow_version_id
        )
        if version is None:
            raise NotFound("运行版本不存在")
        workflow = await self.repo.get_workflow(tenant_id, version.workflow_id)
        if workflow is None:
            raise NotFound("运行所属工作流不存在")

        nodes = await self.repo.list_node_runs(tenant_id, run.id)
        return RunContext(
            run=run,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            workflow_version=version.version,
            dsl=version.dsl,
            nodes=order_node_runs(nodes, version.dsl),
        )

    @staticmethod
    def logs(ctx: RunContext) -> list[WorkflowNodeRun]:
        """节点级时间线；没有节点记录时就是空数组，不编造。"""
        return list(ctx.nodes)

    # ----------------------------------------------------------------- cancel
    async def cancel(
        self,
        run_id: int,
        *,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RunContext:
        ctx = await self.detail(run_id)
        run = ctx.run
        if run.status not in NON_TERMINAL_STATUSES:
            raise Conflict("运行已结束，无法取消")

        previous = run.status
        now = datetime.now(timezone.utc)
        run.status = "cancelled"
        run.finished_at = now
        for node in ctx.nodes:
            if node.status in NON_TERMINAL_STATUSES:
                node.status = "skipped"
                node.finished_at = now
        await self.session.commit()

        await AuditService.write(
            self.session,
            action="workflow.cancel",
            resource_type="workflow_run",
            resource_id=run_id,
            detail={
                "run_id": run_id,
                "workflow_id": ctx.workflow_id,
                "from_status": previous,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        _close_run_trace(run, "cancelled", name=f"workflow.{ctx.workflow_name}")
        return await self.detail(run_id)

    # --------------------------------------------------------------- approval
    async def decide(
        self,
        run_id: int,
        *,
        approve: bool,
        comment: str | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RunContext:
        """批准/拒绝当前等待中的审批节点；角色必须命中节点 approvers。"""
        ctx = await self.detail(run_id)
        run = ctx.run

        # 串行化并发审批：先锁运行行，再锁等待中的节点行。后到的事务会因锁
        # 等待而读到先到者提交后的新状态，于是继续走原有的 409 冲突分支。
        locked_run = await self.session.scalar(
            select(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.tenant_id == get_tenant_id(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_run is not None:
            run = locked_run
        await self.session.scalars(
            select(WorkflowNodeRun)
            .where(
                WorkflowNodeRun.run_id == run_id,
                WorkflowNodeRun.tenant_id == run.tenant_id,
                WorkflowNodeRun.status == "waiting_approval",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        # populate_existing 已刷新同一 identity map 上的 run / nodes，重算等待节点。
        waiting = [
            node for node in ctx.nodes if node.status == "waiting_approval"
        ]
        if run.status != "waiting_approval" or not waiting:
            raise Conflict("运行不在等待审批状态")

        node = waiting[0]
        approval = dict(node.approval) if isinstance(node.approval, dict) else {}
        approvers = approval.get("approvers")
        role = getattr(user, "role", None)
        if not isinstance(approvers, list) or role not in approvers:
            # 管理员不是隐式审批人：必须显式出现在 approvers 里。
            raise Forbidden("当前角色无权审批该节点")

        now = datetime.now(timezone.utc)
        decided = dict(approval)
        decided["decision"] = "approved" if approve else "rejected"
        decided["by"] = getattr(user, "id", None)
        decided["at"] = _iso(now)
        decided["comment"] = comment
        node.approval = decided

        if approve:
            node.status = "succeeded"
            node.output = {"decision": "approved"}
            node.error = None
            node.finished_at = now
            node.heartbeat_at = now
            run.status = "running"
            run.finished_at = None
            run.error = None
            await self.session.commit()

            await AuditService.write(
                self.session,
                action="workflow.approve",
                resource_type="workflow_run",
                resource_id=run_id,
                detail={
                    "run_id": run_id,
                    "node_id": node.node_id,
                    "decision": "approved",
                    "role": role,
                },
                user=user,
                ip=ip,
                user_agent=user_agent,
            )
            try:
                enqueue_workflow_run(run_id, node.node_id)
            except Exception:  # noqa: BLE001 - 入队失败不破坏审批响应
                logger.warning(
                    "审批通过后入队失败（run_id=%s）", run_id, exc_info=True
                )
            return await self.detail(run_id)

        node.status = "failed"
        node.error = "approval_rejected"
        node.output = None
        node.finished_at = now
        node.heartbeat_at = now
        run.status = "failed"
        run.error = "approval_rejected"
        run.finished_at = now
        for other in ctx.nodes:
            if other.status in NON_TERMINAL_STATUSES:
                other.status = "skipped"
                other.finished_at = now
        await self.session.commit()
        _close_run_trace(run, "failed", name=f"workflow.{ctx.workflow_name}")

        await AuditService.write(
            self.session,
            action="workflow.reject",
            resource_type="workflow_run",
            resource_id=run_id,
            detail={
                "run_id": run_id,
                "node_id": node.node_id,
                "decision": "rejected",
                "role": role,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return await self.detail(run_id)


async def expire_waiting_approvals(
    session: AsyncSession, *, now: datetime | None = None
) -> list[int]:
    """把已过期的等待审批节点收敛为失败；按运行逐条提交并审计。"""
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)

    waiting_rows = (
        await session.scalars(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.status == "waiting_approval"
            )
        )
    ).all()

    expired_by_run: dict[int, list[WorkflowNodeRun]] = {}
    for row in waiting_rows:
        approval = row.approval if isinstance(row.approval, dict) else {}
        expires_at = _parse_iso(approval.get("expires_at"))
        if expires_at is None or expires_at >= now_value:
            continue
        expired_by_run.setdefault(row.run_id, []).append(row)

    expired_runs: list[int] = []
    for run_id in sorted(expired_by_run):
        # 跳过已被其它事务锁定的运行；拿到锁后重读并复核状态，避免与并发
        # 审批/取消互相覆盖（已被处理的行不再算作超时）。
        run = await session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if run is None or run.status != "waiting_approval":
            continue
        token = set_tenant_id(run.tenant_id)
        try:
            expired_nodes = expired_by_run[run_id]
            expired_ids = {node.node_id for node in expired_nodes}
            now_for_run = now_value
            for node in expired_nodes:
                node.status = "failed"
                node.error = "approval_timeout"
                node.finished_at = now_for_run
                node.heartbeat_at = now_for_run
            rows = (
                await session.scalars(
                    select(WorkflowNodeRun).where(
                        WorkflowNodeRun.run_id == run_id
                    )
                )
            ).all()
            for row in rows:
                if row.node_id in expired_ids:
                    continue
                if row.status in NON_TERMINAL_STATUSES:
                    row.status = "skipped"
                    row.finished_at = now_for_run

            run.status = "failed"
            run.error = "approval_timeout"
            run.finished_at = now_for_run
            await session.commit()
            _close_run_trace(run, "failed")

            await AuditService.write(
                session,
                action="workflow.approval_timeout",
                resource_type="workflow_run",
                resource_id=run_id,
                detail={
                    "run_id": run_id,
                    "node_ids": sorted(expired_ids),
                    "decision": "timeout",
                },
            )
            expired_runs.append(run_id)
        finally:
            reset_tenant_id(token)
    return expired_runs


async def find_stale_runs(
    session: AsyncSession,
    *,
    older_than_s: int = 60,
    now: datetime | None = None,
) -> list[int]:
    """返回心跳（或无节点时 started_at/created_at）早于阈值的 running 运行。"""
    now_value = now or datetime.now(timezone.utc)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=timezone.utc)
    cutoff = now_value - timedelta(seconds=older_than_s)
    last_seen = func.max(
        func.coalesce(
            WorkflowNodeRun.heartbeat_at,
            WorkflowRun.started_at,
            WorkflowRun.created_at,
        )
    )
    rows = (
        await session.scalars(
            select(WorkflowRun.id)
            .outerjoin(WorkflowNodeRun, WorkflowNodeRun.run_id == WorkflowRun.id)
            .where(WorkflowRun.status == "running")
            .group_by(WorkflowRun.id)
            .having(last_seen < cutoff)
            .order_by(WorkflowRun.id)
        )
    ).all()
    return [int(row) for row in rows]


__all__ = [
    "NON_TERMINAL_STATUSES",
    "RunContext",
    "RunListRow",
    "RunService",
    "enqueue_workflow_run",
    "expire_waiting_approvals",
    "find_stale_runs",
    "order_node_runs",
]
