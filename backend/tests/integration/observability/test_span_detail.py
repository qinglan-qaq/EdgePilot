"""Span 详情扩展：LLM / Tool / Retrieval 关联记录与成本可用标记。"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.conftest import TENANT, auth_headers
from tests.integration.observability.conftest import AGENT_ID, ROOT_ID

SPAN_URL = "/api/v1/observability/spans"


async def _seed_attempt_rows(session) -> None:
    """为 AGENT_ID 写入两次 attempt 的 tool/llm 记录（与 seeded_trace 一样不提交）。"""
    for attempt, status in ((1, "succeeded"), (2, "failed")):
        await session.execute(
            text(
                "INSERT INTO tool_calls (span_id, tenant_id, attempt, tool_id,"
                " tool_name, execution_status)"
                " VALUES (:s, :ten, :a, 'device.ping', 'ping', :st)"
            ),
            {"s": AGENT_ID, "ten": TENANT, "a": attempt, "st": status},
        )
    for attempt, cost in ((1, Decimal("0.5")), (2, None)):
        await session.execute(
            text(
                "INSERT INTO llm_calls (span_id, tenant_id, attempt, provider,"
                " model_name, cost)"
                " VALUES (:s, :ten, :a, 'mock', 'mock-model', :c)"
            ),
            {"s": AGENT_ID, "ten": TENANT, "a": attempt, "c": cost},
        )


@pytest.mark.asyncio
async def test_span_detail_returns_tool_calls_per_attempt(
    client, viewer_token, seeded_trace, session
):
    await _seed_attempt_rows(session)

    r = await client.get(f"{SPAN_URL}/{AGENT_ID}", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0

    data = body["data"]
    assert len(data["tool_calls"]) == 2
    assert [row["attempt"] for row in data["tool_calls"]] == [1, 2]
    assert [row["execution_status"] for row in data["tool_calls"]] == [
        "succeeded",
        "failed",
    ]

    assert len(data["llm_calls"]) == 2
    first, second = data["llm_calls"]
    assert first["attempt"] == 1
    assert first["cost_available"] is True
    assert first["cost"] is not None
    assert float(first["cost"]) == 0.5
    assert second["attempt"] == 2
    assert second["cost_available"] is False
    assert second["cost"] is None

    # 0004 迁移未应用：检索表不存在时必须返回空数组而不是报错。
    assert data["retrieval_records"] == []


@pytest.mark.asyncio
async def test_span_detail_cross_tenant_is_404(
    client, other_tenant_token, seeded_trace
):
    r = await client.get(f"{SPAN_URL}/{AGENT_ID}", headers=auth_headers(other_tenant_token))
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_span_detail_no_related_records(client, viewer_token, seeded_trace):
    r = await client.get(f"{SPAN_URL}/{ROOT_ID}", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["llm_calls"] == []
    assert data["tool_calls"] == []
    assert data["retrieval_records"] == []


@pytest.mark.asyncio
async def test_span_detail_unknown_span_is_404(client, viewer_token, seeded_trace):
    r = await client.get(f"{SPAN_URL}/{'f' * 16}", headers=auth_headers(viewer_token))
    assert r.status_code == 404
