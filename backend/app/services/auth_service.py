"""认证业务逻辑：登录、刷新、登出。

登录发生在租户上下文建立之前，因此按 email 全局查用户；令牌签发成功后
才设置租户上下文。审计写入失败绝不能影响登录主流程。
"""

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import Unauthorized
from app.core.security import create_token, decode_token, verify_password
from app.core.tenancy import set_tenant_id
from app.core.token_denylist import deny, is_denied
from app.models.user import User
from app.schemas.auth import TokenPair
from app.services.audit_service import AuditService


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def login(
        self,
        email: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenPair:
        """校验凭证并签发访问/刷新令牌。

        关键节点：
            1. 账号不存在与密码错误返回同一错误，避免枚举账号；
            2. 登录成功后才设置租户上下文，后续审计与签发都按该租户收口；
            3. 审计失败不影响登录结果（由审计服务自行处理）。

        Args:
            email: 登录邮箱。
            password: 明文密码，仅用于与 argon2 哈希比对。
            ip: 客户端 IP，仅审计使用。
            user_agent: 客户端 User-Agent，仅审计使用。

        Returns:
            新签发的令牌对。

        Raises:
            Unauthorized: 账号不存在、被禁用或密码错误。
        """
        user = await self.session.scalar(
            select(User).where(User.email == email, User.status == "active").limit(1)
        )
        # 不区分账号不存在与密码错误，避免泄露账号是否存在。
        if user is None or not verify_password(user.password_hash, password):
            raise Unauthorized("邮箱或密码错误")

        set_tenant_id(user.tenant_id)
        pair = self._issue_pair(user)
        await self._audit_login(user, ip=ip, user_agent=user_agent)
        return pair

    async def refresh(self, refresh_token: str) -> TokenPair:
        """用刷新令牌换新令牌对。

        关键节点：
            1. 校验令牌类型必须是 refresh，防止拿 access 令牌续期；
            2. 用户必须仍存在且 active，且 jti 不在拒绝名单；
            3. 角色与租户以数据库为准，不信任令牌内嵌的 role。

        Args:
            refresh_token: 客户端持有的刷新令牌。

        Returns:
            新签发的令牌对。

        Raises:
            Unauthorized: 令牌无效、类型错误、用户失效或 jti 已被拒绝。
        """
        try:
            payload = decode_token(refresh_token)
        except Exception:
            raise Unauthorized("令牌无效") from None

        if payload.get("typ") != "refresh":
            raise Unauthorized("令牌类型错误")

        try:
            user_id = int(payload["sub"])
        except (KeyError, TypeError, ValueError):
            raise Unauthorized("令牌无效") from None

        user = await self.session.get(User, user_id)
        if user is None or user.status != "active":
            raise Unauthorized("令牌无效")

        if await is_denied(payload.get("jti")):
            raise Unauthorized("令牌已失效")

        # 以库中的 role/tenant 为准，不信任令牌里的 role。
        set_tenant_id(user.tenant_id)
        return self._issue_pair(user)

    async def logout(self, access_token: str) -> None:
        try:
            payload = decode_token(access_token)
        except Exception:
            return  # 幂等：令牌本身无效时无需处理
        jti = payload.get("jti")
        if not jti:
            return

        ttl = int(payload.get("exp", 0)) - int(time.time())
        max_ttl = get_settings().access_token_minutes * 60
        await deny(jti, max(1, min(ttl, max_ttl)))

    def _issue_pair(self, user: User) -> TokenPair:
        sub = str(user.id)
        tid = str(user.tenant_id)
        return TokenPair(
            access_token=create_token(sub=sub, tenant_id=tid, role=user.role, kind="access"),
            refresh_token=create_token(sub=sub, tenant_id=tid, role=user.role, kind="refresh"),
        )

    async def _audit_login(
        self, user: User, *, ip: str | None, user_agent: str | None
    ) -> None:
        await AuditService.write(
            self.session,
            action="auth.login",
            resource_type="user",
            resource_id=user.id,
            detail={"email": user.email},
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
