"""Analytics API 集成测试。

数据全部用 SQL 直接写入（不经过 tracer），断言以设计稿 §8 的口径为准，
部分关键数字同时与直接 SQL 汇总对照。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.conftest import OTHER_TENANT, TENANT, auth_headers

BASE = "/api/v1/observability/analytics"


async def _get(client, token, path, **params):
    response = await client.get(
        f"{BASE}{path}", params=params, headers=auth_headers(token)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    return body["data"]


@pytest.fixture
async def analytics_seed(session):
    """固定的一批 trace/span/llm/tool 数据，覆盖所有口径分支。"""
    now = datetime.now(timezone.utc)
    recent = now - timedelta(minutes=5)
    old = now - timedelta(hours=30)
    other_recent = now - timedelta(minutes=10)

    traces = [
        # trace_id, name, status, start_time, duration_ms
        ("trace-ok", "device.diagnose", "succeeded", recent, 100.0),
        ("trace-fail", "device.diagnose", "failed", recent, 300.0),
        ("trace-running", "device.diagnose", "running", recent, None),
        ("trace-old", "device.diagnose", "timeout", old, 200.0),
    ]
    for trace_id, name, status, start, duration in traces:
        await session.execute(
            text(
                "INSERT INTO traces (trace_id, tenant_id, name, source, status,"
                " start_time, duration_ms)"
                " VALUES (:trace_id, :tenant_id, :name, 'test', :status,"
                " :start_time, :duration_ms)"
            ),
            {
                "trace_id": trace_id,
                "tenant_id": TENANT,
                "name": name,
                "status": status,
                "start_time": start,
                "duration_ms": duration,
            },
        )

    spans = [
        # span_id, trace_id, parent, name, type, status, attempt, duration,
        # retry, error, agent_id, tool_id
        ("span-wf", "trace-ok", None, "workflow.run", "workflow",
         "succeeded", 1, 50.0, 0, None, None, None),
        ("span-agent-ok", "trace-ok", "span-wf", "agent.device", "agent",
         "succeeded", 1, 100.0, 0, None, 1, None),
        ("span-agent-fail", "trace-fail", "span-wf", "agent.device", "agent",
         "failed", 1, 300.0, 0, None, 1, None),
        ("span-tool-ok", "trace-ok", "span-agent-ok", "tool.device_status",
         "tool", "succeeded", 1, 20.0, 0, None, None, "device.status"),
        ("span-tool-fail", "trace-fail", "span-agent-fail", "tool.device_status",
         "tool", "failed", 1, 40.0, 2, {"code": "TOOL_TIMEOUT"}, None,
         "device.status"),
        ("span-tool-retry", "trace-fail", "span-agent-fail", "tool.device_status",
         "tool", "failed", 2, 40.0, 3, None, None, "device.status"),
        ("span-llm", "trace-ok", "span-agent-ok", "llm.chat", "llm",
         "succeeded", 1, 80.0, 0, {"code": "LLM_RATE_LIMIT"}, None, None),
        ("span-tool-run", "trace-fail", "span-agent-fail", "tool.other",
         "tool", "running", 1, None, 0, None, None, "other.tool"),
        ("span-tool-unk", "trace-fail", "span-agent-fail", "tool.other",
         "tool", "failed", 1, None, 0, {}, None, "other.tool"),
    ]
    for (span_id, trace_id, parent, name, span_type, status, attempt,
         duration, retry, error, agent_id, tool_id) in spans:
        await session.execute(
            text(
                "INSERT INTO spans (span_id, tenant_id, trace_id, parent_span_id,"
                " name, span_type, agent_id, tool_id, status, attempt, start_time,"
                " duration_ms, retry_count, error)"
                " VALUES (:span_id, :tenant_id, :trace_id, :parent_span_id, :name,"
                " :span_type, :agent_id, :tool_id, :status, :attempt, :start_time,"
                " :duration_ms, :retry_count, CAST(:error AS jsonb))"
            ),
            {
                "span_id": span_id,
                "tenant_id": TENANT,
                "trace_id": trace_id,
                "parent_span_id": parent,
                "name": name,
                "span_type": span_type,
                "agent_id": agent_id,
                "tool_id": tool_id,
                "status": status,
                "attempt": attempt,
                "start_time": recent,
                "duration_ms": duration,
                "retry_count": retry,
                "error": None if error is None else json.dumps(error),
            },
        )

    llm_calls = [
        # span_id, provider, model, attempt, prompt, completion, cached, total,
        # cost, error, created_at
        ("span-agent-ok", "openai", "gpt-4o", 1, 100, 50, 25, 150,
         0.015, None, recent),
        ("span-agent-ok", "openai", "gpt-4o", 2, 100, 50, 0, 150,
         0.015, {"code": "rate_limited"}, recent),
        ("span-agent-fail", "anthropic", "claude", 1, 50, None, None, None,
         None, None, recent),
        ("span-agent-fail", "anthropic", "claude", 2, 50, 10, None, 60,
         None, None, recent),
        ("span-agent-fail", "openai", "gpt-4o-mini", 1, None, None, None, None,
         None, None, recent),
        ("span-llm", "openai", "gpt-4o", 1, 500, 500, 0, 1000,
         1.0, None, old),
    ]
    for (span_id, provider, model, attempt, prompt_tokens, completion_tokens,
         cached_tokens, total_tokens, cost, error, created_at) in llm_calls:
        await session.execute(
            text(
                "INSERT INTO llm_calls (span_id, tenant_id, provider, model_name,"
                " attempt, prompt_tokens, completion_tokens, cached_tokens,"
                " total_tokens, cost, error, created_at)"
                " VALUES (:span_id, :tenant_id, :provider, :model_name, :attempt,"
                " :prompt_tokens, :completion_tokens, :cached_tokens,"
                " :total_tokens, :cost, CAST(:error AS jsonb), :created_at)"
            ),
            {
                "span_id": span_id,
                "tenant_id": TENANT,
                "provider": provider,
                "model_name": model,
                "attempt": attempt,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": total_tokens,
                "cost": cost,
                "error": None if error is None else json.dumps(error),
                "created_at": created_at,
            },
        )

    tool_calls = [
        # span_id, tool_id, tool_name, attempt, execution_status, permission
        ("span-tool-ok", "device.status", "tool.device_status", 1,
         "succeeded", "allowed"),
        ("span-tool-fail", "device.status", "tool.device_status", 2,
         "failed", "denied"),
        ("span-tool-fail", "device.status", "tool.device_status", 2,
         "failed", "denied"),
        ("span-tool-run", "other.tool", "tool.other", 1,
         "running", None),
        ("span-agent-ok", "device.status", "tool.device_status", 1,
         "succeeded", "allowed"),
        ("span-agent-ok", "device.status", "tool.device_status", 1,
         "succeeded", "allowed"),
    ]
    for span_id, tool_id, tool_name, attempt, execution, permission in tool_calls:
        await session.execute(
            text(
                "INSERT INTO tool_calls (span_id, tenant_id, tool_id, tool_name,"
                " attempt, execution_status, permission_result, created_at)"
                " VALUES (:span_id, :tenant_id, :tool_id, :tool_name, :attempt,"
                " :execution_status, :permission_result, :created_at)"
            ),
            {
                "span_id": span_id,
                "tenant_id": TENANT,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "attempt": attempt,
                "execution_status": execution,
                "permission_result": permission,
                "created_at": recent,
            },
        )

    # 另一个租户的数据：任何聚合都不能带出来。
    await session.execute(
        text(
            "INSERT INTO traces (trace_id, tenant_id, name, source, status,"
            " start_time, duration_ms)"
            " VALUES ('trace-other', :tenant_id, 'other', 'test', 'succeeded',"
            " :start_time, 50)"
        ),
        {"tenant_id": OTHER_TENANT, "start_time": other_recent},
    )
    await session.execute(
        text(
            "INSERT INTO spans (span_id, tenant_id, trace_id, name, span_type,"
            " status, start_time, duration_ms)"
            " VALUES ('span-other', :tenant_id, 'trace-other', 'agent.other',"
            " 'agent', 'succeeded', :start_time, 50)"
        ),
        {"tenant_id": OTHER_TENANT, "start_time": other_recent},
    )
    await session.execute(
        text(
            "INSERT INTO llm_calls (span_id, tenant_id, provider, model_name,"
            " prompt_tokens, completion_tokens, total_tokens, cost, created_at)"
            " VALUES ('span-other', :tenant_id, 'other-provider', 'other-model',"
            " 500, 499, 999, 9.99, :created_at)"
        ),
        {"tenant_id": OTHER_TENANT, "created_at": other_recent},
    )
    await session.execute(
        text(
            "INSERT INTO tool_calls (span_id, tenant_id, tool_id, tool_name,"
            " execution_status, permission_result, created_at)"
            " VALUES ('span-other', :tenant_id, 'other.tool', 'tool.other',"
            " 'failed', 'denied', :created_at)"
        ),
        {"tenant_id": OTHER_TENANT, "created_at": other_recent},
    )
    return {"recent": recent, "old": old}


@pytest.mark.asyncio
async def test_overview_cards_and_charts(client, viewer_token, analytics_seed, session):
    data = await _get(client, viewer_token, "/overview")

    window = data["window"]
    assert window["bucket"] == "hour"
    start = datetime.fromisoformat(window["start_from"])
    end = datetime.fromisoformat(window["start_to"])
    assert end - start == timedelta(hours=24)

    cards = data["cards"]
    assert cards["traces_total"] == 3
    assert cards["trace_success_rate"] == pytest.approx(0.5)
    assert cards["trace_p95_ms"] == pytest.approx(290.0)
    assert cards["spans_total"] == 9
    assert cards["agent_runs"] == 2
    assert cards["tool_success_rate"] == pytest.approx(1 / 3)
    assert cards["tool_permission_denied"] == 2
    assert cards["llm_calls"] == 5
    assert cards["total_tokens"] == 360
    assert cards["total_cost"] == pytest.approx(0.03)
    assert cards["cost_unavailable_count"] == 3
    assert cards["cost_coverage"] == {"priced_models": 1, "total_models": 3}

    direct_tokens = await session.scalar(
        text(
            "SELECT SUM(total_tokens) FROM llm_calls"
            " WHERE tenant_id = :tenant_id AND created_at >= :start_from"
        ),
        {"tenant_id": TENANT, "start_from": start},
    )
    assert cards["total_tokens"] == direct_tokens

    charts = data["charts"]
    assert sum(b["total"] for b in charts["trace_trend"]) == 3
    assert sum(b["succeeded"] for b in charts["trace_trend"]) == 1
    assert sum(b["failed"] for b in charts["trace_trend"]) == 1
    assert len(charts["trace_trend"]) == 1

    assert charts["latency_trend"] == [
        {
            "bucket": charts["latency_trend"][0]["bucket"],
            "p50_ms": pytest.approx(200.0),
            "p95_ms": pytest.approx(290.0),
        }
    ]
    assert sum(b["total_tokens"] for b in charts["token_trend"]) == 360
    assert sum(b["cached_tokens"] for b in charts["token_trend"]) == 25

    models = {
        (m["provider"], m["model_name"]): m
        for m in charts["model_distribution"]
    }
    assert set(models) == {
        ("openai", "gpt-4o"),
        ("anthropic", "claude"),
        ("openai", "gpt-4o-mini"),
    }
    assert models[("openai", "gpt-4o")]["calls"] == 2
    assert models[("openai", "gpt-4o")]["total_tokens"] == 300
    assert models[("openai", "gpt-4o")]["cost"] == pytest.approx(0.03)
    assert models[("openai", "gpt-4o")]["cost_unavailable_count"] == 0
    assert models[("anthropic", "claude")]["cost"] is None
    assert models[("anthropic", "claude")]["cost_unavailable_count"] == 2

    assert charts["agent_distribution"] == [
        {
            "label": "agent.device",
            "runs": 2,
            "failed": 1,
            "p95_ms": pytest.approx(290.0),
        }
    ]

    tools = {t["tool_id"]: t for t in charts["tool_distribution"]}
    assert tools["device.status"]["calls"] == 2
    assert tools["device.status"]["success_rate"] == pytest.approx(0.5)
    assert tools["device.status"]["p95_ms"] == pytest.approx(39.0)
    assert tools["other.tool"]["calls"] == 2
    assert tools["other.tool"]["success_rate"] == pytest.approx(0.0)
    assert tools["other.tool"]["p95_ms"] is None

    errors = {e["code"]: e["count"] for e in charts["error_distribution"]}
    assert errors == {"unknown": 1, "TOOL_TIMEOUT": 1, "LLM_RATE_LIMIT": 1}

    assert [t["trace_id"] for t in charts["slowest_traces"]] == [
        "trace-fail",
        "trace-ok",
    ]
    assert charts["slowest_traces"][0]["duration_ms"] == pytest.approx(300.0)


@pytest.mark.asyncio
async def test_latency_endpoint(client, viewer_token, analytics_seed):
    data = await _get(client, viewer_token, "/latency")

    assert data["trace"]["count"] == 3
    assert data["trace"]["p50_ms"] == pytest.approx(200.0)
    assert data["trace"]["p95_ms"] == pytest.approx(290.0)

    by_type = {item["span_type"]: item for item in data["by_span_type"]}
    assert by_type["workflow"]["count"] == 1
    assert by_type["workflow"]["p50_ms"] == pytest.approx(50.0)
    assert by_type["agent"]["count"] == 2
    assert by_type["agent"]["p95_ms"] == pytest.approx(290.0)
    assert by_type["tool"]["count"] == 5  # 包含 attempt=2 的物理 span
    assert by_type["tool"]["p50_ms"] == pytest.approx(40.0)
    assert by_type["tool"]["p95_ms"] == pytest.approx(40.0)
    assert by_type["llm"]["count"] == 1


@pytest.mark.asyncio
async def test_tokens_endpoint(client, viewer_token, analytics_seed):
    data = await _get(client, viewer_token, "/tokens")

    assert data["total_tokens"] == 360
    assert data["prompt_tokens"] == 300
    assert data["completion_tokens"] == 110
    assert data["cached_tokens"] == 25
    assert data["cached_ratio"] == pytest.approx(25 / 360)
    assert data["unavailable_calls"] == 2  # total_tokens 为 NULL 的两条

    by_model = {
        (m["provider"], m["model_name"]): m for m in data["by_model"]
    }
    assert by_model[("openai", "gpt-4o")]["calls"] == 2
    assert by_model[("openai", "gpt-4o")]["total_tokens"] == 300
    assert by_model[("openai", "gpt-4o")]["cached_tokens"] == 25
    assert by_model[("anthropic", "claude")]["total_tokens"] == 60
    assert by_model[("openai", "gpt-4o-mini")]["total_tokens"] is None


@pytest.mark.asyncio
async def test_cost_endpoint_and_coverage(client, viewer_token, analytics_seed):
    data = await _get(client, viewer_token, "/cost")

    assert data["total_cost"] == pytest.approx(0.03)
    assert data["cost_unavailable_count"] == 3
    assert data["coverage"] == {"priced_models": 1, "total_models": 3}

    by_model = {
        (m["provider"], m["model_name"]): m for m in data["by_model"]
    }
    assert by_model[("openai", "gpt-4o")]["cost"] == pytest.approx(0.03)
    assert by_model[("openai", "gpt-4o")]["cost_unavailable_count"] == 0
    assert by_model[("anthropic", "claude")]["cost"] is None
    assert by_model[("anthropic", "claude")]["cost_unavailable_count"] == 2
    assert by_model[("openai", "gpt-4o-mini")]["cost"] is None


@pytest.mark.asyncio
async def test_cost_is_null_when_all_costs_missing(client, viewer_token, session):
    await session.execute(
        text(
            "INSERT INTO llm_calls (span_id, tenant_id, provider, model_name,"
            " total_tokens) VALUES ('span-unpriced', :tenant_id, 'local',"
            " 'llama', 42)"
        ),
        {"tenant_id": TENANT},
    )
    data = await _get(client, viewer_token, "/cost")

    assert data["total_cost"] is None
    assert data["cost_unavailable_count"] == 1
    assert data["coverage"] == {"priced_models": 0, "total_models": 1}
    assert data["by_model"][0]["cost"] is None
    assert data["by_model"][0]["cost_unavailable_count"] == 1


@pytest.mark.asyncio
async def test_agents_endpoint(client, viewer_token, analytics_seed):
    data = await _get(client, viewer_token, "/agents")

    assert data["items"] == [
        {
            "agent_id": 1,
            "label": "agent.device",
            "runs": 2,
            "failed": 1,
            "success_rate": pytest.approx(0.5),
            "avg_duration_ms": pytest.approx(200.0),
            "p95_duration_ms": pytest.approx(290.0),
            "total_tokens": 360,
            "tool_calls": 6,
        }
    ]


@pytest.mark.asyncio
async def test_agent_tool_usage_counts_descendant_tool_spans(
    client, viewer_token, analytics_seed, session
):
    """生产数据把 tool_calls 挂在 tool span 上，Agent 维度必须沿父子关系汇总。"""
    recent = analytics_seed["recent"]
    await session.execute(
        text(
            "INSERT INTO traces (trace_id, tenant_id, name, source, status,"
            " start_time, duration_ms)"
            " VALUES ('trace-agent-tool', :tenant_id, 'agent.tool', 'test',"
            " 'succeeded', :start_time, 10)"
        ),
        {"tenant_id": TENANT, "start_time": recent},
    )
    for span_id, parent, name, span_type in [
        ("sp-agent-tools", None, "agent.tools", "agent"),
        ("sp-agent-llm", "sp-agent-tools", "llm.chat", "llm"),
        ("sp-agent-tool", "sp-agent-llm", "tool.device_status", "tool"),
    ]:
        await session.execute(
            text(
                "INSERT INTO spans (span_id, tenant_id, trace_id, parent_span_id,"
                " name, span_type, status, attempt, start_time, duration_ms)"
                " VALUES (:span_id, :tenant_id, 'trace-agent-tool', :parent, :name,"
                " :span_type, 'succeeded', 1, :start_time, 10)"
            ),
            {
                "span_id": span_id,
                "tenant_id": TENANT,
                "parent": parent,
                "name": name,
                "span_type": span_type,
                "start_time": recent,
            },
        )
    await session.execute(
        text(
            "INSERT INTO tool_calls (span_id, tenant_id, tool_id, tool_name,"
            " attempt, execution_status, permission_result, created_at)"
            " VALUES ('sp-agent-tool', :tenant_id, 'device.status',"
            " 'tool.device_status', 1, 'succeeded', 'allowed', :created_at)"
        ),
        {"tenant_id": TENANT, "created_at": recent},
    )

    data = await _get(client, viewer_token, "/agents")
    agent = next(item for item in data["items"] if item["label"] == "agent.tools")
    assert agent["runs"] == 1
    assert agent["tool_calls"] == 1


@pytest.mark.asyncio
async def test_tools_endpoint_retry_and_permission_denied(
    client, viewer_token, analytics_seed
):
    data = await _get(client, viewer_token, "/tools")
    items = {t["tool_id"]: t for t in data["items"]}

    device = items["device.status"]
    assert device["tool_name"] == "tool.device_status"
    assert device["calls"] == 2  # 仅 attempt=1 的逻辑 span
    assert device["success_rate"] == pytest.approx(0.5)
    assert device["p50_ms"] == pytest.approx(30.0)
    assert device["p95_ms"] == pytest.approx(39.0)
    assert device["retry_count"] == 2  # 只累计逻辑 span 的 retry_count
    assert device["permission_denied"] == 2

    other = items["other.tool"]
    assert other["calls"] == 2
    assert other["success_rate"] == pytest.approx(0.0)
    assert other["p50_ms"] is None
    assert other["p95_ms"] is None
    assert other["retry_count"] == 0
    assert other["permission_denied"] == 0


@pytest.mark.asyncio
async def test_models_endpoint(client, viewer_token, analytics_seed):
    data = await _get(client, viewer_token, "/models")
    items = {(m["provider"], m["model_name"]): m for m in data["items"]}

    assert set(items) == {
        ("openai", "gpt-4o"),
        ("anthropic", "claude"),
        ("openai", "gpt-4o-mini"),
    }
    gpt4o = items[("openai", "gpt-4o")]
    assert gpt4o["calls"] == 2
    assert gpt4o["prompt_tokens"] == 200
    assert gpt4o["completion_tokens"] == 100
    assert gpt4o["cached_tokens"] == 25
    assert gpt4o["total_tokens"] == 300
    assert gpt4o["cost"] == pytest.approx(0.03)
    assert gpt4o["cost_unavailable_count"] == 0
    assert gpt4o["errors"] == 1

    claude = items[("anthropic", "claude")]
    assert claude["cost"] is None
    assert claude["cost_unavailable_count"] == 2
    assert claude["errors"] == 0


@pytest.mark.asyncio
async def test_cross_tenant_isolation(client, viewer_token, other_tenant_token,
                                      analytics_seed):
    viewer = await _get(client, viewer_token, "/overview")
    cards = viewer["cards"]
    assert cards["traces_total"] == 3
    assert cards["spans_total"] == 9
    assert cards["llm_calls"] == 5
    assert cards["total_tokens"] == 360
    assert cards["total_cost"] == pytest.approx(0.03)
    assert cards["tool_permission_denied"] == 2

    models = await _get(client, viewer_token, "/models")
    assert "other-provider" not in {m["provider"] for m in models["items"]}

    other = await _get(client, other_tenant_token, "/overview")
    other_cards = other["cards"]
    assert other_cards["traces_total"] == 1
    assert other_cards["spans_total"] == 1
    assert other_cards["agent_runs"] == 1
    assert other_cards["llm_calls"] == 1
    assert other_cards["total_tokens"] == 999
    assert other_cards["total_cost"] == pytest.approx(9.99)
    assert other_cards["tool_permission_denied"] == 1
    assert other_cards["cost_coverage"] == {"priced_models": 1, "total_models": 1}


@pytest.mark.asyncio
async def test_default_window_is_last_24_hours(client, viewer_token,
                                               analytics_seed):
    now = datetime.now(timezone.utc)
    wide_from = (now - timedelta(hours=31)).isoformat()

    default = await _get(client, viewer_token, "/overview")
    assert default["cards"]["traces_total"] == 3
    assert default["cards"]["llm_calls"] == 5
    assert default["cards"]["total_tokens"] == 360

    wide = await _get(client, viewer_token, "/overview", start_from=wide_from)
    assert wide["window"]["bucket"] == "hour"
    assert wide["cards"]["traces_total"] == 4  # 30 小时前的 trace-old
    assert wide["cards"]["llm_calls"] == 6
    assert wide["cards"]["total_tokens"] == 1360
    assert wide["cards"]["total_cost"] == pytest.approx(1.03)

    daily = await _get(
        client, viewer_token, "/overview",
        start_from=(now - timedelta(hours=100)).isoformat(),
    )
    assert daily["window"]["bucket"] == "day"
    assert daily["cards"]["traces_total"] == 4


@pytest.mark.asyncio
async def test_empty_tenant_returns_zero_and_nulls(client, viewer_token):
    data = await _get(client, viewer_token, "/overview")
    cards = data["cards"]
    assert cards["traces_total"] == 0
    assert cards["spans_total"] == 0
    assert cards["agent_runs"] == 0
    assert cards["llm_calls"] == 0
    assert cards["tool_permission_denied"] == 0
    assert cards["trace_success_rate"] is None
    assert cards["tool_success_rate"] is None
    assert cards["trace_p95_ms"] is None
    assert cards["total_tokens"] is None
    assert cards["total_cost"] is None
    assert cards["cost_coverage"] == {"priced_models": 0, "total_models": 0}
    assert all(value == [] for value in data["charts"].values())

    tokens = await _get(client, viewer_token, "/tokens")
    assert tokens["total_tokens"] is None
    assert tokens["cached_ratio"] is None
    assert tokens["by_model"] == []

    for path in ("/latency", "/cost", "/agents", "/tools", "/models"):
        await _get(client, viewer_token, path)


@pytest.mark.asyncio
async def test_invalid_datetime_is_422(client, viewer_token):
    response = await client.get(
        f"{BASE}/overview",
        params={"start_from": "not-a-datetime"},
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 422
    assert response.json()["code"] == 422


@pytest.mark.asyncio
async def test_inverted_window_is_422(client, viewer_token):
    response = await client.get(
        f"{BASE}/overview",
        params={
            "start_from": "2026-01-02T00:00:00+00:00",
            "start_to": "2026-01-01T00:00:00+00:00",
        },
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 422
