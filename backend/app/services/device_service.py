"""设备业务逻辑：租户内 CRUD 与 probe 指令。"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound, ServiceUnavailable
from app.core.tenancy import get_tenant_id
from app.messaging.mqtt.topics import cmd_topic
from app.messaging.mqtt.traceparent import resolve_traceparent
from app.models.device import Device
from app.repositories.device_repo import DeviceRepo
from app.repositories.node_repo import NodeRepo
from app.services.audit_service import AuditService
from app.services.node_service import node_is_disabled

logger = logging.getLogger(__name__)


class DeviceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DeviceRepo(session)
        self.nodes = NodeRepo(session)

    async def list(
        self,
        page: int = 1,
        size: int = 20,
        *,
        node_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[Device], int]:
        return await self.repo.list_devices(page, size, node_id=node_id, status=status)

    async def get(self, device_id: int) -> Device:
        return await self.repo.get_or_404(device_id)

    async def create(
        self,
        *,
        node_id: int,
        name: str,
        kind: str = "camera",
        config: dict | None = None,
        status: str = "offline",
    ) -> Device:
        # 节点必须属于当前租户，否则统一 404。
        await self.nodes.get_or_404(node_id)
        row = Device(
            tenant_id=get_tenant_id(),
            node_id=node_id,
            name=name,
            kind=kind,
            config=config or {},
            status=status,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def update(self, device_id: int, **fields: Any) -> Device:
        row = await self.get(device_id)
        for key in ("name", "kind", "config", "status"):
            if key in fields and fields[key] is not None:
                setattr(row, key, fields[key])
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def delete(self, device_id: int) -> None:
        row = await self.get(device_id)
        await self.session.delete(row)
        await self.session.commit()

    async def probe(
        self,
        device_id: int,
        *,
        publisher,
        task_id: str | None = None,
        traceparent: str | None = None,
        header_traceparent: str | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        device = await self.get(device_id)
        node = await self.nodes.get_or_404(device.node_id)
        if node_is_disabled(node):
            raise Conflict("设备所属节点已禁用，不能探测")
        if not node.node_key:
            raise NotFound("节点不存在")

        resolved_task_id = task_id or str(uuid.uuid4())
        resolved_traceparent = resolve_traceparent(traceparent, header_traceparent)
        body = {
            "device_id": device.id,
            "config": device.config or {},
            "task_id": resolved_task_id,
            "traceparent": resolved_traceparent,
        }
        try:
            published = await publisher.publish_command(
                node.node_key, "probe_device", body, qos=1
            )
        except Exception:  # noqa: BLE001 - 发布失败统一转 503
            logger.exception("设备探测发布异常 device_id=%s", device_id)
            raise ServiceUnavailable("MQTT 服务不可用") from None
        if not published:
            raise ServiceUnavailable("MQTT 服务不可用")

        topic = cmd_topic(node.node_key, "probe_device")
        await AuditService.write(
            self.session,
            action="device.probe",
            resource_type="device",
            resource_id=device.id,
            detail={
                "node_key": node.node_key,
                "task_id": resolved_task_id,
                "traceparent": resolved_traceparent,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return {
            "accepted": True,
            "device_id": device.id,
            "node_key": node.node_key,
            "task_id": resolved_task_id,
            "traceparent": resolved_traceparent,
            "topic": topic,
        }
