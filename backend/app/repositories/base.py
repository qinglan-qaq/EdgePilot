from typing import Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_id
from app.models.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def select(self, *where) -> Select:
        stmt = select(self.model).where(self.model.tenant_id == get_tenant_id())
        if where:
            stmt = stmt.where(*where)
        return stmt

    async def get_or_404(self, pk: int) -> ModelT:
        from app.core.exceptions import NotFound

        obj = await self.session.scalar(self.select(self.model.id == pk))
        if obj is None:
            raise NotFound()
        return obj

    async def count(self, *where) -> int:
        stmt = select(func.count()).select_from(self.model).where(
            self.model.tenant_id == get_tenant_id()
        )
        if where:
            stmt = stmt.where(*where)
        return await self.session.scalar(stmt) or 0
