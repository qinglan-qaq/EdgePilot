"""Vision 数据访问：任务、结果、事件、模型与工作流运行关联。

所有查询显式带 ``tenant_id``；只有轮询器使用的 ``list_active`` 跨租户，
但每条任务仍会在使用前设置该任务的租户上下文。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import VisionEvent
from app.models.run import WorkflowRun
from app.models.vision import VisionModel, VisionResult, VisionTask


class VisionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ tasks
    async def get_task(
        self, tenant_id: uuid.UUID, task_uid: str, *, for_update: bool = False
    ) -> VisionTask | None:
        stmt = select(VisionTask).where(
            VisionTask.tenant_id == tenant_id,
            VisionTask.task_uid == task_uid,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_task_by_id(
        self, tenant_id: uuid.UUID, task_id: int, *, for_update: bool = False
    ) -> VisionTask | None:
        stmt = select(VisionTask).where(
            VisionTask.tenant_id == tenant_id, VisionTask.id == task_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    async def get_task_by_uid(self, task_uid: str) -> VisionTask | None:
        """按 task_uid 跨租户查找，仅供媒体签名路由解析任务租户。

        签名 URL 的 token 自带租户绑定，路由拿到任务后再用 ``verify_media_token``
        校验；这里不做鉴权，绝不提供给业务查询使用。
        """
        return await self.session.scalar(
            select(VisionTask)
            .where(VisionTask.task_uid == task_uid)
            .order_by(VisionTask.id)
            .limit(1)
        )

    async def get_by_dedup(self, tenant_id: uuid.UUID, dedup_key: str) -> VisionTask | None:
        return await self.session.scalar(
            select(VisionTask).where(
                VisionTask.tenant_id == tenant_id,
                VisionTask.dedup_key == dedup_key,
            )
        )

    def create_task(self, **fields: Any) -> VisionTask:
        task = VisionTask(**fields)
        self.session.add(task)
        return task

    async def list_tasks(
        self,
        tenant_id: uuid.UUID,
        *,
        page: int,
        size: int,
        status: str | None = None,
        query: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> tuple[list[tuple[VisionTask, VisionResult | None]], int]:
        conditions = [VisionTask.tenant_id == tenant_id]
        if status:
            conditions.append(VisionTask.status == status)
        if query:
            conditions.append(VisionTask.video_filename.ilike(f"%{query}%"))
        if created_after is not None:
            conditions.append(VisionTask.created_at >= created_after)
        if created_before is not None:
            conditions.append(VisionTask.created_at <= created_before)

        total = (
            await self.session.scalar(
                select(func.count()).select_from(VisionTask).where(*conditions)
            )
            or 0
        )
        rows = await self.session.execute(
            select(VisionTask, VisionResult)
            .outerjoin(
                VisionResult,
                and_(
                    VisionResult.task_id == VisionTask.id,
                    VisionResult.tenant_id == VisionTask.tenant_id,
                ),
            )
            .where(*conditions)
            .order_by(VisionTask.created_at.desc(), VisionTask.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(rows.all()), total

    async def list_active(self) -> list[VisionTask]:
        rows = await self.session.scalars(
            select(VisionTask)
            .where(VisionTask.status.in_(("pending", "running")))
            .order_by(VisionTask.created_at)
        )
        return list(rows.all())

    async def pending_never_dispatched(self) -> list[VisionTask]:
        rows = await self.session.scalars(
            select(VisionTask).where(
                VisionTask.status == "pending", VisionTask.dispatch_attempts == 0
            )
        )
        return list(rows.all())

    async def list_expired(self, cutoff: datetime) -> list[VisionTask]:
        rows = await self.session.scalars(
            select(VisionTask)
            .where(VisionTask.created_at < cutoff, VisionTask.status.in_(
                ("completed", "failed", "cancelled")
            ))
            .order_by(VisionTask.created_at)
        )
        return list(rows.all())

    # ----------------------------------------------------------------- results
    async def get_result(
        self, tenant_id: uuid.UUID, task_id: int, *, for_update: bool = False
    ) -> VisionResult | None:
        stmt = select(VisionResult).where(
            VisionResult.tenant_id == tenant_id, VisionResult.task_id == task_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await self.session.scalar(stmt)

    # 新建行时必须显式给出的 NOT NULL 列（其余有 server_default）。
    _RESULT_REQUIRED_FIELDS = frozenset(
        {"task_uid", "artifact_bytes", "result_sha256", "frame_count"}
    )

    async def upsert_result(
        self, tenant_id: uuid.UUID, task_id: int, **fields: Any
    ) -> VisionResult:
        """按 (tenant_id, task_id) 幂等写入，不 rollback 调用方事务。

        先 UPDATE 再按需 INSERT：PostgreSQL 会在冲突判定前校验待插入行的
        NOT NULL 约束，因此只带部分字段的增量更新无法走 ``INSERT ... ON
        CONFLICT``；先更新命中时可直接完成。行不存在时才要求字段完整，并用
        ``ON CONFLICT DO UPDATE`` 兜底并发首写。
        """
        if not fields:
            existing = await self.get_result(tenant_id, task_id)
            if existing is None:
                raise ValueError("新建 vision_results 至少需要一个字段")
            return existing
        values = {"tenant_id": tenant_id, "task_id": task_id, **fields}
        update_stmt = (
            update(VisionResult)
            .where(
                VisionResult.tenant_id == tenant_id,
                VisionResult.task_id == task_id,
            )
            .values(**fields)
            .returning(VisionResult)
        )
        # populate_existing：用 RETURNING 的新值覆盖 identity map 中的旧实例。
        existing = (
            await self.session.execute(
                update_stmt, execution_options={"populate_existing": True}
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        missing = self._RESULT_REQUIRED_FIELDS - fields.keys()
        if missing:
            raise ValueError(
                f"新建 vision_results 缺少必填字段: {', '.join(sorted(missing))}"
            )
        statement = pg_insert(VisionResult).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=["tenant_id", "task_id"],
            set_={key: getattr(statement.excluded, key) for key in fields},
        ).returning(VisionResult)
        return (
            await self.session.execute(
                statement, execution_options={"populate_existing": True}
            )
        ).scalar_one()

    # ------------------------------------------------------------------ events
    def _event_filter(self, tenant_id: uuid.UUID, task_uid: str):
        return (
            VisionEvent.tenant_id == tenant_id,
            VisionEvent.payload["task_uid"].astext == task_uid,
        )

    async def list_task_events(
        self, tenant_id: uuid.UUID, task_uid: str, *, page: int, size: int
    ) -> tuple[list[VisionEvent], int]:
        conditions = self._event_filter(tenant_id, task_uid)
        total = (
            await self.session.scalar(
                select(func.count()).select_from(VisionEvent).where(*conditions)
            )
            or 0
        )
        rows = await self.session.scalars(
            select(VisionEvent)
            .where(*conditions)
            .order_by(VisionEvent.occurred_at.desc(), VisionEvent.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(rows.all()), total

    async def task_event_ids(self, tenant_id: uuid.UUID, task_uid: str) -> list[int]:
        rows = await self.session.scalars(
            select(VisionEvent.id)
            .where(*self._event_filter(tenant_id, task_uid))
            .order_by(VisionEvent.occurred_at, VisionEvent.id)
        )
        return [int(row) for row in rows.all()]

    async def latest_event(self, tenant_id: uuid.UUID, task_uid: str) -> VisionEvent | None:
        return await self.session.scalar(
            select(VisionEvent)
            .where(*self._event_filter(tenant_id, task_uid))
            .order_by(VisionEvent.occurred_at.desc(), VisionEvent.id.desc())
            .limit(1)
        )

    async def get_event(self, tenant_id: uuid.UUID, event_id: int) -> VisionEvent | None:
        return await self.session.scalar(
            select(VisionEvent).where(
                VisionEvent.tenant_id == tenant_id, VisionEvent.id == event_id
            )
        )

    # ------------------------------------------------------------------ models
    async def list_models(self, tenant_id: uuid.UUID) -> list[VisionModel]:
        rows = await self.session.scalars(
            select(VisionModel)
            .where(VisionModel.tenant_id == tenant_id)
            .order_by(VisionModel.id)
        )
        return list(rows.all())

    async def get_model(self, tenant_id: uuid.UUID, model_key: str) -> VisionModel | None:
        return await self.session.scalar(
            select(VisionModel).where(
                VisionModel.tenant_id == tenant_id, VisionModel.model_key == model_key
            )
        )

    async def upsert_model(self, tenant_id: uuid.UUID, model_key: str, **fields: Any) -> VisionModel:
        model = await self.get_model(tenant_id, model_key)
        if model is None:
            model = VisionModel(tenant_id=tenant_id, model_key=model_key, **fields)
            self.session.add(model)
        else:
            for key, value in fields.items():
                setattr(model, key, value)
        await self.session.flush()
        return model

    # --------------------------------------------------------------- workflow
    async def find_run_by_vision_task(
        self, tenant_id: uuid.UUID, task_uid: str
    ) -> WorkflowRun | None:
        return await self.session.scalar(
            select(WorkflowRun).where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.trigger_type == "event",
                WorkflowRun.trigger_payload["vision_task_uid"].astext == task_uid,
            )
        )

    # ---------------------------------------------------------------- overview
    async def overview(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        task_row = (
            await self.session.execute(
                select(
                    func.count(VisionTask.id),
                    func.count(VisionTask.id).filter(VisionTask.status == "completed"),
                    func.count(VisionTask.id).filter(VisionTask.status == "failed"),
                    func.count(VisionTask.id).filter(VisionTask.status == "running"),
                    func.coalesce(func.sum(VisionTask.processed_frames), 0),
                    func.coalesce(func.sum(VisionTask.detection_count), 0),
                ).where(VisionTask.tenant_id == tenant_id)
            )
        ).one()
        event_count = (
            await self.session.scalar(
                select(func.coalesce(func.sum(VisionResult.event_count), 0)).where(
                    VisionResult.tenant_id == tenant_id
                )
            )
            or 0
        )
        recent = (
            await self.session.scalars(
                select(VisionTask)
                .where(VisionTask.tenant_id == tenant_id)
                .order_by(VisionTask.created_at.desc(), VisionTask.id.desc())
                .limit(5)
            )
        ).all()
        return {
            "total_tasks": int(task_row[0] or 0),
            "completed": int(task_row[1] or 0),
            "failed": int(task_row[2] or 0),
            "running": int(task_row[3] or 0),
            "processed_frames": int(task_row[4] or 0),
            "detection_count": int(task_row[5] or 0),
            "event_count": int(event_count or 0),
            "recent": list(recent),
        }
