"""设备路由；读接口登录可用，写接口 admin/operator。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.messaging.mqtt.client import get_command_publisher
from app.models.user import User
from app.schemas.common import ok
from app.schemas.device import (
    DeviceCreate,
    DeviceOut,
    DeviceProbeIn,
    DeviceProbeOut,
    DeviceUpdate,
)
from app.services.device_service import DeviceService

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("")
async def list_devices(
    params: PageParams = Depends(),
    node_id: int | None = None,
    status: str | None = None,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await DeviceService(session).list(
        params.page, params.size, node_id=node_id, status=status
    )
    page = PageResult[DeviceOut](
        items=[DeviceOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.post("")
async def create_device(
    payload: DeviceCreate,
    _: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await DeviceService(session).create(
        node_id=payload.node_id,
        name=payload.name,
        kind=payload.kind,
        config=payload.config,
        status=payload.status,
    )
    return ok(DeviceOut.model_validate(row).model_dump())


@router.patch("/{device_id}")
async def update_device(
    device_id: int,
    payload: DeviceUpdate,
    _: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await DeviceService(session).update(
        device_id, **payload.model_dump(exclude_unset=True)
    )
    return ok(DeviceOut.model_validate(row).model_dump())


@router.delete("/{device_id}")
async def delete_device(
    device_id: int,
    _: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    await DeviceService(session).delete(device_id)
    return ok(None)


@router.post("/{device_id}/probe")
async def probe_device(
    device_id: int,
    request: Request,
    payload: DeviceProbeIn | None = None,
    user: User = Depends(require_role("admin", "operator")),
    publisher=Depends(get_command_publisher),
    session: AsyncSession = Depends(get_db),
) -> dict:
    body = payload or DeviceProbeIn()
    result = await DeviceService(session).probe(
        device_id,
        publisher=publisher,
        task_id=body.task_id,
        traceparent=body.traceparent,
        header_traceparent=request.headers.get("traceparent"),
        user=user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(DeviceProbeOut.model_validate(result).model_dump())
