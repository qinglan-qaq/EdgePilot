"""事件业务逻辑：筛选查询、ack 状态机与快照路径校验。

读接口所有登录角色可用；ack 仅 admin/operator，且写旁路审计 ``event.ack``
（重复 ack 幂等，不重复写审计）。跨租户与不存在统一 404。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.models.event import VisionEvent
from app.repositories.event_repo import EventRepo
from app.services.audit_service import AuditService
from app.vision.snapshot import resolve_snapshot_path

IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class EventService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = EventRepo(session)

    async def list(
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
        return await self.repo.list_events(
            page,
            size,
            start=start,
            end=end,
            node_id=node_id,
            device_id=device_id,
            event_type=event_type,
            severity=severity,
            status=status,
        )

    async def get(self, event_id: int) -> VisionEvent:
        return await self.repo.get_or_404(event_id)

    async def ack(
        self,
        event_id: int,
        *,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> VisionEvent:
        row = await self.get(event_id)
        if row.status != "new":
            # 已 ack 幂等返回；其它状态不擅自改写。
            return row

        previous = row.status
        row.status = "acked"
        await self.session.commit()
        await self.session.refresh(row)
        await AuditService.write(
            self.session,
            action="event.ack",
            resource_type="vision_event",
            resource_id=row.id,
            detail={"from": previous, "to": "acked"},
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return row

    def snapshot_file(self, row: VisionEvent, storage_dir: str | Path) -> tuple[Path, str]:
        """返回快照绝对路径与 media type；无快照/越界/文件缺失统一 404。"""
        if not row.snapshot_path:
            raise NotFound("事件没有快照")
        try:
            path = resolve_snapshot_path(storage_dir, row.snapshot_path)
        except ValueError:
            raise NotFound("快照不存在") from None
        if not path.is_file():
            raise NotFound("快照不存在")
        media_type = IMAGE_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return path, media_type
