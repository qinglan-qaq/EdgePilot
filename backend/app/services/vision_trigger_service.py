"""Vision 完成后的工作流触发：任务级幂等。

一个视觉任务最多只对应一条 ``trigger_type='event'`` 的运行：

1. 先按 ``trigger_payload->>'vision_task_uid'`` 查既有运行，命中即复用；
2. 只有 ``completed`` 且存在事件的任务才允许新建运行；
3. 工作流名来自配置，必须是已发布且存在发布版本；
4. ``workflow_runs`` 上的部分唯一索引 ``uq_workflow_runs_vision_task`` 兜底并发。

失败只写 ``vision_results.trigger_status/trigger_error``，绝不回退任务状态、
不抛给轮询器，也不回滚调用方已落库的结果。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.run import WorkflowRun
from app.models.vision import VisionResult, VisionTask
from app.repositories.vision_repo import VisionRepo
from app.repositories.workflow_repo import WorkflowRepo
from app.services.run_service import RunService

logger = logging.getLogger(__name__)

# trigger_error 列宽 255，超长原因截断，避免写库失败。
_ERROR_LIMIT = 255


@dataclass
class TriggerOutcome:
    """一次联动尝试的结果；``status`` 与 ``vision_results.trigger_status`` 对齐。"""

    status: str
    workflow_run_id: int | None = None
    error: str | None = None


def _error_text(exc: BaseException | str | None) -> str | None:
    if exc is None:
        return None
    text = str(exc).strip()
    if not text:
        text = type(exc).__name__ if isinstance(exc, BaseException) else "触发失败"
    return text[:_ERROR_LIMIT]


def apply_trigger_outcome(
    result: VisionResult,
    status: str,
    workflow_run_id: int | None,
    error: str | None,
) -> None:
    """把一次联动结果写回结果行（不提交）。

    触发服务与结果服务共用这一处字段写入，避免两处逻辑漂移；
    ``triggered_at`` 只在首次真正触发时写，幂等命中保留原值。
    """
    result.trigger_status = status
    result.workflow_run_id = workflow_run_id
    result.trigger_error = error[:_ERROR_LIMIT] if error else None
    if status == "triggered" and result.triggered_at is None:
        result.triggered_at = datetime.now(timezone.utc)


class VisionTriggerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = VisionRepo(session)

    async def trigger(
        self, task: VisionTask, result: VisionResult
    ) -> TriggerOutcome:
        """触发（或复用）该视觉任务的研判工作流，并回写结果行联动字段。

        调用方负责在任务 ``completed`` 提交之后调用；重复调用只命中幂等分支，
        不会创建第二条运行。任何异常都收敛为 ``failed`` 结果。
        """
        settings = get_settings()
        tenant_id = task.tenant_id
        # 先取出稳定标识：后续冲突回查/失败路径不再依赖 task 的 ORM 属性。
        task_uid = task.task_uid

        if task.status != "completed":
            return await self._finish(
                result, TriggerOutcome("skipped", None, f"任务状态 {task.status} 不触发")
            )

        # 幂等优先：重放/轮询/补派发都先看是否已有同一任务的 event 运行。
        existing = await self.repo.find_run_by_vision_task(tenant_id, task_uid)
        if existing is not None:
            return await self._finish(
                result, TriggerOutcome("triggered", existing.id, None)
            )

        event_ids = await self.repo.task_event_ids(tenant_id, task_uid)
        if not event_ids:
            return await self._finish(
                result, TriggerOutcome("skipped", None, "任务没有视觉事件")
            )

        if not settings.vision_trigger_enabled:
            return await self._finish(
                result, TriggerOutcome("skipped", None, "视觉联动已关闭")
            )

        workflow = await WorkflowRepo(self.session).find_published_by_name(
            tenant_id, settings.vision_trigger_workflow_name
        )
        if workflow is None:
            return await self._finish(
                result, TriggerOutcome("skipped", None, "未找到已发布工作流")
            )

        input_data: dict[str, Any] = {
            "task_uid": task_uid,
            "video_filename": task.video_filename,
            "event_ids": event_ids,
            "event_count": len(event_ids),
            "summary": result.summary,
        }
        trigger_payload: dict[str, Any] = {
            "source": "vision",
            "vision_task_id": task.id,
            "vision_task_uid": task_uid,
            "vision_event_ids": event_ids,
            "first_event_id": event_ids[0],
        }

        # RunService 只从租户上下文取 tenant_id；轮询器可能已为别的任务设置过
        # 上下文，这里显式切到本任务租户，结束后恢复。
        token = set_tenant_id(tenant_id)
        # 手动 SAVEPOINT（不用 with）：RunService.create 内部会 commit 根事务，
        # 继续在 ``begin_nested`` 上下文里发命令会触发
        # “Can't operate on closed transaction inside context manager”。
        # 冲突时只回滚本 SAVEPOINT，不调用 session.rollback()，调用方未提交
        # 的写入因此不会被丢弃。
        nested = await self.session.begin_nested()
        try:
            ctx = await RunService(self.session).create(
                workflow.id,
                input_data=input_data,
                trigger_type="event",
                trigger_payload=trigger_payload,
                user=None,
            )
            run_id = int(ctx.run.id)
        except IntegrityError:
            # 唯一索引冲突：SAVEPOINT 已随异常回滚，回查赢家运行并视为成功。
            await self._rollback_nested(nested)
            existing = await self._recover_existing(tenant_id, task_uid)
            if existing is None:
                logger.warning(
                    "视觉联动唯一索引冲突但未找到既有运行 task_uid=%s", task_uid
                )
                return await self._finish(
                    result, TriggerOutcome("failed", None, "运行创建冲突")
                )
            return await self._finish(
                result, TriggerOutcome("triggered", existing.id, None)
            )
        except Exception as exc:  # noqa: BLE001 - 联动失败不能影响任务完成
            await self._rollback_nested(nested)
            logger.warning(
                "视觉联动工作流创建失败 task_uid=%s error=%s",
                task_uid,
                type(exc).__name__,
                exc_info=True,
            )
            return await self._finish(
                result, TriggerOutcome("failed", None, _error_text(exc))
            )
        finally:
            reset_tenant_id(token)

        return await self._finish(
            result, TriggerOutcome("triggered", run_id, None)
        )

    async def _rollback_nested(self, nested) -> None:
        """尽力回滚本 SAVEPOINT；已随 create 的 commit 关闭时静默跳过。

        flush 失败后 SQLAlchemy 会把 SAVEPOINT 标记为 DEACTIVE，但必须调用
        ``rollback()`` 才能把 session 恢复到可继续查询的状态。
        """
        try:
            await nested.rollback()
        except Exception:  # noqa: BLE001 - 已关闭/已回滚都不影响后续回查
            logger.debug("视觉联动 SAVEPOINT 回滚跳过", exc_info=True)

    async def _recover_existing(
        self, tenant_id, task_uid: str
    ) -> WorkflowRun | None:
        """唯一索引冲突后回查既有运行。

        插入已在 SAVEPOINT 内完成，冲突时 SAVEPOINT 随异常自动回滚，session
        仍可继续查询；这里绝不调用 ``session.rollback()``，避免丢弃调用方
        未提交的写入。结果行与任务 ``completed`` 也都已在此前提交。
        """
        try:
            return await self.repo.find_run_by_vision_task(tenant_id, task_uid)
        except Exception:  # noqa: BLE001 - 回查失败按 failed 收敛
            logger.warning("视觉联动冲突后回查运行失败 task_uid=%s", task_uid)
            return None

    async def _finish(
        self, result: VisionResult, outcome: TriggerOutcome
    ) -> TriggerOutcome:
        """把联动结果写回结果行并提交；失败原因截断到列宽。"""
        apply_trigger_outcome(
            result, outcome.status, outcome.workflow_run_id, outcome.error
        )
        await self.session.commit()
        return outcome


__all__ = ["TriggerOutcome", "VisionTriggerService", "apply_trigger_outcome"]
