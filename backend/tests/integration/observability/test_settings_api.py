"""可观测设置 API 集成测试（Task 17）。

覆盖：管理员读取真实配置、非管理员 403、缺价模型只统计本租户且不含伪造行。
数据全部用 SQL 直接写入并共享外层事务，不 commit / rollback。
"""

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.user import User
from tests.conftest import (
    OTHER_TENANT,
    PASSWORD,
    TENANT,
    auth_headers,
    login_token,
)

SETTINGS_URL = "/api/v1/observability/settings"
OPERATOR_EMAIL = "operator@edgepilot.local"


@pytest.fixture
async def operator_token(client, seeded_users, session):
    """require_role 只允许 admin，operator 也应被拒绝；这里单独造一个 operator。"""
    session.add(
        User(
            tenant_id=TENANT,
            email=OPERATOR_EMAIL,
            password_hash=hash_password(PASSWORD),
            display_name="Operator",
            role="operator",
            status="active",
        )
    )
    await session.flush()
    return await login_token(client, OPERATOR_EMAIL)


async def _insert_llm_call(
    session,
    *,
    tenant_id,
    provider: str,
    model_name: str,
    cost: float | None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO llm_calls (tenant_id, span_id, provider, model_name, cost)"
            " VALUES (:tenant_id, :span_id, :provider, :model_name, :cost)"
        ),
        {
            "tenant_id": tenant_id,
            "span_id": "a" * 16,
            "provider": provider,
            "model_name": model_name,
            "cost": cost,
        },
    )


@pytest.mark.asyncio
async def test_settings_admin_returns_real_config(client, admin_token):
    response = await client.get(SETTINGS_URL, headers=auth_headers(admin_token))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    data = body["data"]

    assert set(data) == {
        "capture_mode",
        "capture_modes",
        "retention_days",
        "redaction",
        "unpriced_models",
    }
    # 采集模式必须与当前进程配置一致，不能是前端杜撰的默认值。
    assert data["capture_mode"] == get_settings().obs_capture_mode
    assert data["capture_modes"] == ["OFF", "METADATA", "REDACTED", "FULL"]

    assert data["retention_days"] == {
        "trace_events": 90,
        "traces": 180,
        "spans": 180,
        "llm_calls": 180,
        "tool_calls": 180,
        "retrieval_records": 180,
        "prompt_executions": 180,
        "audit_logs": 365,
    }
    assert all(isinstance(value, int) and value > 0 for value in data["retention_days"].values())

    redaction = data["redaction"]
    assert redaction["sensitive_key_pattern"]
    assert redaction["scrubbers"] == ["bearer", "sk_key", "phone", "id_card"]
    assert redaction["token_count_exempt"] is True

    # 没有 llm_calls 数据时必须返回空列表，不能补齐占位行。
    assert data["unpriced_models"] == []


@pytest.mark.asyncio
async def test_settings_forbidden_for_viewer_and_operator(
    client, viewer_token, operator_token
):
    for token in (viewer_token, operator_token):
        response = await client.get(SETTINGS_URL, headers=auth_headers(token))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == 403


@pytest.mark.asyncio
async def test_unpriced_models_only_counts_missing_cost_in_tenant(
    client, admin_token, session
):
    # 本租户：openai/gpt-4o 两条缺价、一条有价；anthropic/claude 一条缺价。
    await _insert_llm_call(
        session, tenant_id=TENANT, provider="openai", model_name="gpt-4o", cost=None
    )
    await _insert_llm_call(
        session, tenant_id=TENANT, provider="openai", model_name="gpt-4o", cost=None
    )
    await _insert_llm_call(
        session, tenant_id=TENANT, provider="openai", model_name="gpt-4o", cost=0.01
    )
    await _insert_llm_call(
        session, tenant_id=TENANT, provider="anthropic", model_name="claude", cost=None
    )
    # 其他租户大量缺价，绝不能出现在当前租户结果里。
    for _ in range(5):
        await _insert_llm_call(
            session,
            tenant_id=OTHER_TENANT,
            provider="openai",
            model_name="gpt-4o",
            cost=None,
        )
    await _insert_llm_call(
        session, tenant_id=OTHER_TENANT, provider="other", model_name="unpriced", cost=None
    )

    response = await client.get(SETTINGS_URL, headers=auth_headers(admin_token))
    assert response.status_code == 200, response.text
    items = response.json()["data"]["unpriced_models"]

    assert items == [
        {"provider": "openai", "model_name": "gpt-4o", "calls": 2},
        {"provider": "anthropic", "model_name": "claude", "calls": 1},
    ]
    # 有价调用不计入缺价统计；其他租户的 5 次不得混入。
    assert all(item["calls"] != 5 for item in items)
