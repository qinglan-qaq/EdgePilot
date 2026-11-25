"""节点路由（spec §6.2 / §6.3）。

读接口所有登录角色可用；PATCH 与指令下发给 admin/operator，viewer 403。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.messaging.mqtt.client import get_command_publisher
from app.models.user import User
from app.schemas.common import ok
from app.schemas.node import (
    NodeCommandIn,
    NodeCommandOut,
    NodeMetricsOut,
    NodeOut,
    NodeUpdate,
)
from app.services.node_service import NodeService

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.get("")
async def list_nodes(
    params: PageParams = Depends(),
    status: str | None = None,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await NodeService(session).list(params.page, params.size, status=status)
    page = PageResult[NodeOut](
        items=[NodeOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.get("/{node_key}")
async def get_node(
    node_key: str,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await NodeService(session).get(node_key)
    return ok(NodeOut.model_validate(row).model_dump())


@router.get("/{node_key}/metrics")
async def get_node_metrics(
    node_key: str,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await NodeService(session).get(node_key)
    out = NodeMetricsOut(
        node_key=row.node_key,
        status=row.status,
        metrics=row.metrics,
        last_heartbeat_at=row.last_heartbeat_at,
    )
    return ok(out.model_dump())


@router.patch("/{node_key}")
async def update_node(
    node_key: str,
    payload: NodeUpdate,
    _: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    fields = payload.model_dump(exclude_unset=True)
    row = await NodeService(session).update(
        node_key,
        name=fields.get("name"),
        labels=fields.get("labels"),
        disabled=fields.get("disabled"),
    )
    return ok(NodeOut.model_validate(row).model_dump())


@router.post("/{node_key}/commands")
async def send_node_command(
    node_key: str,
    payload: NodeCommandIn,
    request: Request,
    user: User = Depends(require_role("admin", "operator")),
    publisher=Depends(get_command_publisher),
    session: AsyncSession = Depends(get_db),
) -> dict:
    result = await NodeService(session).send_command(
        node_key,
        action=payload.action,
        publisher=publisher,
        task_id=payload.task_id,
        payload=payload.payload,
        traceparent=payload.traceparent,
        header_traceparent=request.headers.get("traceparent"),
        user=user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(NodeCommandOut.model_validate(result).model_dump())
