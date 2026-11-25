"""视觉事件仓储；所有读取自动带 tenant_id。"""

from __future__ import annotations

from datetime import datetime

from app.models.event import VisionEvent
from app.repositories.base import BaseRepository


class EventRepo(BaseRepository[VisionEvent]):
    model = VisionEvent

    async def list_events(
        self,
        page: int = 1,
        size: int = 20,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        node_id: int | None = None,
        device_id: int | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        status: str | None = None,
    ) -> tuple[list[VisionEvent], int]:
        where = []
        if start is not None:
            where.append(VisionEvent.occurred_at >= start)
        if end is not None:
            where.append(VisionEvent.occurred_at <= end)
        if node_id is not None:
            where.append(VisionEvent.node_id == node_id)
        if device_id is not None:
            where.append(VisionEvent.device_id == device_id)
        if event_type is not None:
            where.append(VisionEvent.event_type == event_type)
        if severity is not None:
            where.append(VisionEvent.severity == severity)
        if status is not None:
            where.append(VisionEvent.status == status)

        total = await self.count(*where)
        rows = (
            await self.session.scalars(
                self.select(*where)
                .order_by(VisionEvent.occurred_at.desc(), VisionEvent.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        return list(rows), total
