"""节点业务逻辑：分页查询、禁用/启用、下发指令。

所有查询走 :class:`NodeRepo`（自动带 tenant_id）；跨租户与不存在统一 404。
下行指令必须带 ``task_id`` 与 ``traceparent``，并在成功后写旁路审计。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, ServiceUnavailable
from app.messaging.mqtt.topics import cmd_topic
from app.messaging.mqtt.traceparent import resolve_traceparent
from app.models.device import EdgeNode
from app.repositories.node_repo import NodeRepo
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)


def node_is_disabled(row: EdgeNode) -> bool:
    return row.status == "disabled" or bool((row.labels or {}).get("disabled"))


class NodeService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = NodeRepo(session)

    async def list(
        self, page: int = 1, size: int = 20, *, status: str | None = None
    ) -> tuple[list[EdgeNode], int]:
        return await self.repo.list_nodes(page, size, status=status)

    async def get(self, node_key: str) -> EdgeNode:
        return await self.repo.get_by_key_or_404(node_key)

    async def update(
        self,
        node_key: str,
        *,
        name: str | None = None,
        labels: dict | None = None,
        disabled: bool | None = None,
    ) -> EdgeNode:
        row = await self.get(node_key)

        merged = dict(row.labels or {})
        if labels is not None:
            # disabled 只能通过专用字段写入，避免 labels 绕过状态机。
            merged.update({k: v for k, v in labels.items() if k != "disabled"})
        if disabled is not None:
            merged["disabled"] = bool(disabled)
            row.status = "disabled" if disabled else "offline"
        if name is not None:
            row.name = name
        row.labels = merged

        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def send_command(
        self,
        node_key: str,
        *,
        action: str,
        publisher,
        task_id: str | None = None,
        payload: dict | None = None,
        traceparent: str | None = None,
        header_traceparent: str | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        row = await self.get(node_key)
        if node_is_disabled(row):
            raise Conflict("节点已禁用，不能下发指令")

        resolved_task_id = task_id or str(uuid.uuid4())
        resolved_traceparent = resolve_traceparent(traceparent, header_traceparent)

        body: dict[str, Any] = dict(payload or {})
        # 下行载荷以显式参数为准，禁止 payload 覆盖 task_id/traceparent。
        body["task_id"] = resolved_task_id
        body["traceparent"] = resolved_traceparent

        try:
            published = await publisher.publish_command(
                node_key, action, body, qos=1
            )
        except Exception:  # noqa: BLE001 - 发布失败统一转 503
            logger.exception("节点指令发布异常 node_key=%s action=%s", node_key, action)
            raise ServiceUnavailable("MQTT 服务不可用") from None
        if not published:
            raise ServiceUnavailable("MQTT 服务不可用")

        topic = cmd_topic(node_key, action)
        await AuditService.write(
            self.session,
            action="node.command",
            resource_type="node",
            resource_id=row.id,
            detail={
                "node_key": node_key,
                "action": action,
                "task_id": resolved_task_id,
                "traceparent": resolved_traceparent,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return {
            "accepted": True,
            "node_key": node_key,
            "action": action,
            "task_id": resolved_task_id,
            "traceparent": resolved_traceparent,
            "topic": topic,
        }
