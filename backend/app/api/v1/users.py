"""用户管理端点（仅 admin）。

写审计是旁路：``user.create`` / ``user.update`` 的 ``detail`` 只放
email/role/status 与变更字段名，绝不出现明文或哈希密码。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.user import UserCreate, UserOut, UserRole, UserStatus, UserUpdate
from app.services.audit_service import AuditService
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


@router.get("")
async def list_users(
    params: PageParams = Depends(),
    role: UserRole | None = None,
    status: UserStatus | None = None,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await UserService(session).list(
        params.page, params.size, role=role, status=status
    )
    page = PageResult[UserOut](
        items=[UserOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.post("")
async def create_user(
    payload: UserCreate,
    request: Request,
    admin: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await UserService(session).create(**payload.model_dump())
    ip, user_agent = _client_meta(request)
    await AuditService.write(
        session,
        action="user.create",
        resource_type="user",
        resource_id=row.id,
        detail={"email": row.email, "role": row.role, "status": row.status},
        user=admin,
        ip=ip,
        user_agent=user_agent,
    )
    return ok(UserOut.model_validate(row).model_dump())


@router.patch("/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    # 显式传 null 不代表“清空”：users 的这些列都非空，忽略 null 保持原值。
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    # 审计只记录字段名，密码字段名也一并剔除，避免任何密码线索进入审计。
    changed = sorted(key for key in fields if key != "password")
    row = await UserService(session).update(user_id, **fields)

    ip, user_agent = _client_meta(request)
    await AuditService.write(
        session,
        action="user.update",
        resource_type="user",
        resource_id=row.id,
        detail={
            "email": row.email,
            "role": row.role,
            "status": row.status,
            "changed": changed,
        },
        user=admin,
        ip=ip,
        user_agent=user_agent,
    )
    return ok(UserOut.model_validate(row).model_dump())
