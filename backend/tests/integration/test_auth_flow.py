"""认证端点（登录/刷新/登出/me）与 RBAC 依赖链的集成测试。

注意：viewer 越权发布 agent、跨租户返回 404 这两条依赖 /api/v1/agents 的
测试留待 Task 11；此处用测试内临时路由验证真实的 ``require_role`` /
``get_current_user`` 依赖链。
"""

import pytest
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db import get_db
from app.main import register_exception_handlers
from app.models.audit import AuditLog
from tests.conftest import (
    ADMIN_EMAIL,
    OTHER_ADMIN_EMAIL,
    OTHER_TENANT,
    PASSWORD,
    TENANT,
    auth_headers,
    login_token,
)


async def _redis_available() -> bool:
    client = aioredis.from_url(
        get_settings().redis_url, socket_connect_timeout=0.5, socket_timeout=0.5
    )
    try:
        return bool(await client.ping())
    except Exception:
        return False
    finally:
        await client.aclose()


def _role_app(session) -> FastAPI:
    """只挂 require_role("admin") 的临时 app，用于验证真实依赖链。"""
    from app.api.deps import require_role  # 延迟导入：先写测试再实现依赖模块

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/admin-only")
    async def admin_only(user=Depends(require_role("admin"))) -> dict:
        return {"code": 0, "message": "ok", "data": {"email": user.email}, "trace_id": None}

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    return app


async def test_login_and_me(client, seeded_users):
    r = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0

    access = body["data"]["access_token"]
    r2 = await client.get("/api/v1/auth/me", headers=auth_headers(access))
    assert r2.status_code == 200
    data = r2.json()["data"]
    assert data["role"] == "admin"
    assert data["email"] == ADMIN_EMAIL
    assert data["tenant_id"] == str(TENANT)


async def test_login_rejects_wrong_password(client, seeded_users):
    r = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-password"}
    )
    assert r.status_code == 401
    assert r.json()["code"] == 401


async def test_login_writes_audit_log(client, seeded_users, session):
    r = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert r.json()["code"] == 0

    count = await session.scalar(select(func.count()).select_from(AuditLog))
    assert count == 1
    row = await session.scalar(select(AuditLog))
    assert row.action == "auth.login"
    assert row.resource_type == "user"


async def test_other_tenant_user_gets_own_tenant(client, seeded_users):
    token = await login_token(client, OTHER_ADMIN_EMAIL)
    r = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r.json()["data"]["tenant_id"] == str(OTHER_TENANT)


async def test_refresh_token_cannot_be_used_as_access(client, seeded_users):
    r = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    refresh = r.json()["data"]["refresh_token"]
    r2 = await client.get("/api/v1/auth/me", headers=auth_headers(refresh))
    assert r2.status_code == 401


async def test_refresh_returns_new_pair(client, seeded_users):
    r = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    data = r.json()["data"]
    r2 = await client.post("/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert r2.status_code == 200
    body = r2.json()
    assert body["code"] == 0
    assert body["data"]["access_token"] != data["access_token"]

    r3 = await client.get("/api/v1/auth/me", headers=auth_headers(body["data"]["access_token"]))
    assert r3.status_code == 200
    assert r3.json()["data"]["email"] == ADMIN_EMAIL


async def test_viewer_forbidden_on_admin_route(client, seeded_users, viewer_token, session):
    app = _role_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/admin-only", headers=auth_headers(viewer_token))
    assert r.status_code == 403
    assert r.json()["code"] == 403


async def test_admin_allowed_on_admin_route(client, seeded_users, admin_token, session):
    app = _role_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/admin-only", headers=auth_headers(admin_token))
    assert r.status_code == 200
    assert r.json()["data"]["email"] == ADMIN_EMAIL


async def test_logout_denylists_access_token(client, seeded_users):
    if not await _redis_available():
        pytest.skip("Redis 不可达，跳过令牌 denylist 测试")

    token = await login_token(client, ADMIN_EMAIL)
    r = await client.post("/api/v1/auth/logout", headers=auth_headers(token))
    assert r.status_code == 200
    assert r.json()["code"] == 0

    r2 = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r2.status_code == 401
