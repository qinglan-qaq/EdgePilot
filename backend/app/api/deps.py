"""请求级依赖：当前用户解析与角色校验。"""

import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import decode_token
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.core.token_denylist import is_denied
from app.db import get_db
from app.models.user import User

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise Unauthorized("缺少令牌")
    try:
        payload = decode_token(creds.credentials)
    except Exception:
        raise Unauthorized("令牌无效") from None

    if payload.get("typ") != "access":
        raise Unauthorized("令牌类型错误")
    if await is_denied(payload.get("jti")):
        raise Unauthorized("令牌已失效")

    try:
        tenant_id = uuid.UUID(payload["tid"])
    except (KeyError, TypeError, ValueError):
        raise Unauthorized("令牌无效") from None

    token = set_tenant_id(tenant_id)
    try:
        user = await session.get(User, int(payload["sub"]))
    except Exception:
        reset_tenant_id(token)
        raise Unauthorized("令牌无效") from None

    if user is None or user.status != "active":
        reset_tenant_id(token)
        raise Unauthorized("用户不可用")
    return user


def require_role(*roles: str):
    async def _require_role(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise Forbidden("角色不足")
        return user

    return _require_role
