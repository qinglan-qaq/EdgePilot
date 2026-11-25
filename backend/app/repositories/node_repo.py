"""边缘节点仓储；所有读取由 ``BaseRepository`` 自动带 tenant_id。"""

from __future__ import annotations

from sqlalchemy import select

from app.core.exceptions import NotFound
from app.models.device import EdgeNode
from app.repositories.base import BaseRepository


class NodeRepo(BaseRepository[EdgeNode]):
    model = EdgeNode

    async def get_by_key(self, node_key: str) -> EdgeNode | None:
        return await self.session.scalar(self.select(EdgeNode.node_key == node_key))

    async def get_by_key_or_404(self, node_key: str) -> EdgeNode:
        row = await self.get_by_key(node_key)
        if row is None:
            raise NotFound("节点不存在")
        return row

    async def list_nodes(
        self, page: int = 1, size: int = 20, *, status: str | None = None
    ) -> tuple[list[EdgeNode], int]:
        where = []
        if status is not None:
            where.append(EdgeNode.status == status)
        total = await self.count(*where)
        rows = (
            await self.session.scalars(
                self.select(*where)
                .order_by(EdgeNode.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        return list(rows), total
