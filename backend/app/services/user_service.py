"""用户管理业务逻辑：租户内分页、创建与更新。

约定：

- 所有查询都显式带 ``tenant_id == get_tenant_id()``，跨租户与不存在统一 404；
- 邮箱在租户内唯一，冲突转 409；
- 密码只在服务层哈希，返回值是 ORM 行，由 API 层映射成不含哈希的响应；
- 审计由 API 层旁路写入，服务层不写审计。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.core.security import hash_password
from app.core.tenancy import get_tenant_id
from app.models.user import User

_UPDATE_FIELDS = frozenset({"email", "password", "display_name", "role", "status"})


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self,
        page: int = 1,
        size: int = 20,
        *,
        role: str | None = None,
        status: str | None = None,
    ) -> tuple[list[User], int]:
        filters = [User.tenant_id == get_tenant_id()]
        if role is not None:
            filters.append(User.role == role)
        if status is not None:
            filters.append(User.status == status)

        total = (
            await self.session.scalar(
                select(func.count()).select_from(User).where(*filters)
            )
            or 0
        )
        rows = (
            await self.session.scalars(
                select(User)
                .where(*filters)
                .order_by(User.created_at.desc(), User.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        return list(rows), int(total)

    async def get(self, user_id: int) -> User:
        row = await self.session.scalar(
            select(User).where(
                User.id == user_id,
                User.tenant_id == get_tenant_id(),
            )
        )
        if row is None:
            raise NotFound("用户不存在")
        return row

    async def create(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        role: str = "viewer",
        status: str = "active",
    ) -> User:
        tenant_id = get_tenant_id()
        if await self._find_by_email(tenant_id, email) is not None:
            raise Conflict("邮箱已存在")

        row = User(
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password(password),
            display_name=display_name,
            role=role,
            status=status,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("邮箱已存在") from None

        await self.session.refresh(row)
        return row

    async def update(self, user_id: int, **fields: Any) -> User:
        row = await self.get(user_id)
        tenant_id = get_tenant_id()

        unknown = set(fields) - _UPDATE_FIELDS
        if unknown:
            raise ValueError(f"不支持更新字段: {sorted(unknown)}")

        new_email = fields.get("email")
        if new_email is not None and new_email != row.email:
            existing = await self._find_by_email(tenant_id, new_email)
            if existing is not None and existing.id != row.id:
                raise Conflict("邮箱已存在")

        if "password" in fields:
            fields["password_hash"] = hash_password(fields.pop("password"))
        for key, value in fields.items():
            setattr(row, key, value)

        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("邮箱已存在") from None

        await self.session.refresh(row)
        return row

    async def _find_by_email(self, tenant_id: uuid.UUID, email: str) -> User | None:
        return await self.session.scalar(
            select(User).where(
                User.tenant_id == tenant_id,
                User.email == email,
            )
        )


__all__ = ["UserService"]
