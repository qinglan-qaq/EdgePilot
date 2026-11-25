"""Shared pytest fixtures.

本文件提供数据库/会话/client/种子用户夹具以及认证相关夹具
（auth_headers / login_token / admin_token / viewer_token /
other_tenant_token）。

`migrated_db` 是 session 级 autouse：每次测试会话把测试库 upgrade 到 head。
所有 session 都绑到同一个连接 + 外层事务，测试结束整体回滚。
"""

import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import get_db
from app.main import create_app
from app.models.user import User

PASSWORD = "Passw0rd!"
ADMIN_EMAIL = "admin@edgepilot.local"
VIEWER_EMAIL = "viewer@edgepilot.local"
OTHER_ADMIN_EMAIL = "other_tenant_admin@edgepilot.local"
TENANT = uuid.UUID(get_settings().tenant_default_id)
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """测试库建表一次。alembic/env.py 读 DATABASE_URL，所以临时指向 test 库。"""
    from alembic import command
    from alembic.config import Config

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = get_settings().test_database_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


@pytest.fixture
async def db_conn(migrated_db):
    """所有 session 都绑到同一个连接 + 外层事务，测试结束整体回滚。"""
    engine = create_async_engine(get_settings().test_database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            yield conn
        finally:
            await tx.rollback()
    await engine.dispose()


@pytest.fixture
async def session(db_conn):
    async with async_sessionmaker(bind=db_conn, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
async def session_factory(db_conn):
    return async_sessionmaker(bind=db_conn, expire_on_commit=False)


@pytest.fixture
async def client(session):
    app = create_app()

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
async def seeded_users(session):
    """同一租户 admin/viewer + 另一租户 admin；只 flush，外层事务统一回滚。"""
    users = [
        User(tenant_id=TENANT, email=ADMIN_EMAIL, password_hash=hash_password(PASSWORD),
             display_name="Admin", role="admin", status="active"),
        User(tenant_id=TENANT, email=VIEWER_EMAIL, password_hash=hash_password(PASSWORD),
             display_name="Viewer", role="viewer", status="active"),
        User(tenant_id=OTHER_TENANT, email=OTHER_ADMIN_EMAIL,
             password_hash=hash_password(PASSWORD), display_name="Other", role="admin",
             status="active"),
    ]
    session.add_all(users)
    await session.flush()
    return users


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def login_token(client, email: str, password: str = PASSWORD) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return r.json()["data"]["access_token"]


@pytest.fixture
async def admin_token(client, seeded_users):
    return await login_token(client, ADMIN_EMAIL)


@pytest.fixture
async def viewer_token(client, seeded_users):
    return await login_token(client, VIEWER_EMAIL)


@pytest.fixture
async def other_tenant_token(client, seeded_users):
    return await login_token(client, OTHER_ADMIN_EMAIL)
