"""事件路由（spec §6.2 / §6.3）。

读接口所有登录角色可用；ack 限 admin/operator，viewer 403。快照走
``FileResponse`` 图片流，路径必须落在配置的 storage_dir 内。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.event import EventOut
from app.services.event_service import EventService

router = APIRouter(prefix="/events", tags=["events"])


def get_storage_dir() -> str:
    """截图根目录；测试可用 ``app.dependency_overrides`` 覆盖为 tmp_path。"""
    return get_settings().storage_dir


@router.get("")
async def list_events(
    params: PageParams = Depends(),
    start: datetime | None = None,
    end: datetime | None = None,
    node_id: int | None = None,
    device_id: int | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await EventService(session).list(
        params.page,
        params.size,
        start=start,
        end=end,
        node_id=node_id,
        device_id=device_id,
        event_type=event_type,
        severity=severity,
        status=status,
    )
    page = PageResult[EventOut](
        items=[EventOut.from_row(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.get("/{event_id}")
async def get_event(
    event_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await EventService(session).get(event_id)
    return ok(EventOut.from_row(row).model_dump())


@router.post("/{event_id}/ack")
async def ack_event(
    event_id: int,
    request: Request,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await EventService(session).ack(
        event_id,
        user=user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(EventOut.from_row(row).model_dump())


@router.get("/{event_id}/snapshot")
async def get_event_snapshot(
    event_id: int,
    storage_dir: str = Depends(get_storage_dir),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    service = EventService(session)
    row = await service.get(event_id)
    path, media_type = service.snapshot_file(row, storage_dir)
    return FileResponse(path, media_type=media_type)
