"""设备仓储；所有读取自动带 tenant_id。"""

from __future__ import annotations

from app.models.device import Device
from app.repositories.base import BaseRepository


class DeviceRepo(BaseRepository[Device]):
    model = Device

    async def list_devices(
        self,
        page: int = 1,
        size: int = 20,
        *,
        node_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[Device], int]:
        where = []
        if node_id is not None:
            where.append(Device.node_id == node_id)
        if status is not None:
            where.append(Device.status == status)
        total = await self.count(*where)
        rows = (
            await self.session.scalars(
                self.select(*where)
                .order_by(Device.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        return list(rows), total

    async def get_by_node_and_id(self, node_id: int, device_id: int) -> Device | None:
        return await self.session.scalar(
            self.select(Device.id == device_id, Device.node_id == node_id)
        )
