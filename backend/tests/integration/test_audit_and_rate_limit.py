"""审计查询端点与 Redis 固定窗口限流的集成测试。"""

import uuid

import pytest
from sqlalchemy import delete

from app.core.exceptions import RateLimited
from app.core.rate_limit import limit
from app.models.audit import AuditLog
from app.services.audit_service import AuditService
from tests.conftest import auth_headers


async def test_audit_logs_admin_can_list(client, seeded_users, admin_token, session):
    users = seeded_users
    # admin_token 登录本身会写一条 auth.login 审计；先清空以断言本次写入的 2 条。
    await session.execute(delete(AuditLog))

    await AuditService.write(
        session,
        action="agent.publish",
        resource_type="agent",
        resource_id=1,
        detail={"k": "v"},
        user=users[0],
    )
    await AuditService.write(
        session,
        action="agent.publish",
        resource_type="agent",
        resource_id=2,
        detail={"k": "v"},
        user=users[0],
    )

    r = await client.get(
        "/api/v1/audit-logs?page=1&size=10", headers=auth_headers(admin_token)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["size"] == 10
    assert data["items"][0]["action"] == "agent.publish"
    # created_at 倒序；同一事务内 now() 相同，由 id desc 打破平局。
    assert data["items"][0]["id"] > data["items"][1]["id"]
    assert data["items"][0]["created_at"] >= data["items"][1]["created_at"]


async def test_audit_logs_viewer_forbidden(client, seeded_users, viewer_token):
    r = await client.get("/api/v1/audit-logs", headers=auth_headers(viewer_token))
    assert r.status_code == 403
    assert r.json()["code"] == 403


async def test_rate_limit_raises_after_times():
    key = f"test:rl:{uuid.uuid4()}"
    await limit(key, times=2, seconds=60)
    await limit(key, times=2, seconds=60)
    with pytest.raises(RateLimited):
        await limit(key, times=2, seconds=60)


async def test_rate_limit_fail_open_when_redis_down(monkeypatch):
    import app.core.rate_limit as rate_limit

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(rate_limit, "_client", _boom)
    # 限流基础设施不可用时不得阻断业务。
    await rate_limit.limit("x", times=1, seconds=1)
