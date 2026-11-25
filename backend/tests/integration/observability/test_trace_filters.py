"""Trace 列表新增过滤条件的集成测试。

覆盖设计稿 §9 的 source / user_id / agent_id / model / tool / failed / slow，
以及组合条件、分页与跨租户隔离。测试与其它 integration 用例共享外层事务，
因此全部用原生 SQL 直接插表，不对采集管道做任何假设。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.conftest import OTHER_TENANT, TENANT, auth_headers

BASE_TIME = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)


def _trace_id() -> str:
    return uuid.uuid4().hex


def _span_id() -> str:
    # span_id 列上限 16 字符。
    return uuid.uuid4().hex[:16]


async def _seed_trace(
    session,
    *,
    tenant_id=TENANT,
    name: str = "trace",
    source: str = "test",
    status: str = "succeeded",
    duration_ms: float | None = 100.0,
    user_id: str | None = None,
    start_offset: int = 0,
) -> str:
    trace_id = _trace_id()
    await session.execute(
        text(
            "INSERT INTO traces (trace_id, tenant_id, name, source, user_id, status,"
            " duration_ms, total_spans, start_time)"
            " VALUES (:t, :ten, :name, :source, :user_id, :status, :duration, 1, :start)"
        ),
        {
            "t": trace_id,
            "ten": tenant_id,
            "name": name,
            "source": source,
            "user_id": user_id,
            "status": status,
            "duration": duration_ms,
            "start": BASE_TIME + timedelta(seconds=start_offset),
        },
    )
    return trace_id


async def _seed_span(
    session,
    trace_id: str,
    *,
    tenant_id=TENANT,
    span_type: str = "workflow",
    agent_id: int | None = None,
    tool_id: str | None = None,
) -> str:
    span_id = _span_id()
    await session.execute(
        text(
            "INSERT INTO spans (span_id, tenant_id, trace_id, name, span_type, agent_id,"
            " tool_id, status, start_time)"
            " VALUES (:s, :ten, :t, :name, :span_type, :agent_id, :tool_id, 'succeeded', :start)"
        ),
        {
            "s": span_id,
            "ten": tenant_id,
            "t": trace_id,
            "name": f"{span_type}-span",
            "span_type": span_type,
            "agent_id": agent_id,
            "tool_id": tool_id,
            "start": BASE_TIME,
        },
    )
    return span_id


async def _seed_llm_call(
    session,
    span_id: str,
    *,
    tenant_id=TENANT,
    provider: str = "openai",
    model_name: str = "gpt-4o",
) -> None:
    await session.execute(
        text(
            "INSERT INTO llm_calls (span_id, tenant_id, provider, model_name)"
            " VALUES (:s, :ten, :provider, :model)"
        ),
        {
            "s": span_id,
            "ten": tenant_id,
            "provider": provider,
            "model": model_name,
        },
    )


async def _seed_tool_call(
    session,
    span_id: str,
    *,
    tenant_id=TENANT,
    tool_id: str = "camera.snapshot",
    tool_name: str = "抓帧",
) -> None:
    await session.execute(
        text(
            "INSERT INTO tool_calls (span_id, tenant_id, tool_id, tool_name, execution_status)"
            " VALUES (:s, :ten, :tool_id, :tool_name, 'succeeded')"
        ),
        {
            "s": span_id,
            "ten": tenant_id,
            "tool_id": tool_id,
            "tool_name": tool_name,
        },
    )


async def _list_traces(client, token, **params) -> dict:
    response = await client.get(
        "/api/v1/observability/traces",
        params=params,
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 0, payload
    return payload["data"]


def _trace_ids(page: dict) -> set[str]:
    return {item["trace_id"] for item in page["items"]}


@pytest.mark.asyncio
async def test_source_and_user_id_are_exact_matches(client, viewer_token, session):
    workflow_trace = await _seed_trace(
        session, source="workflow", user_id="user-1", start_offset=1
    )
    playground_trace = await _seed_trace(
        session, source="playground", user_id="user-2", start_offset=2
    )

    by_source = await _list_traces(client, viewer_token, source="workflow")
    assert _trace_ids(by_source) == {workflow_trace}

    by_prefix_like_value = await _list_traces(client, viewer_token, source="work")
    assert by_prefix_like_value["total"] == 0

    by_user = await _list_traces(client, viewer_token, user_id="user-2")
    assert _trace_ids(by_user) == {playground_trace}


@pytest.mark.asyncio
async def test_agent_id_requires_agent_span(client, viewer_token, session):
    agent_trace = await _seed_trace(session, start_offset=1)
    await _seed_span(session, agent_trace, span_type="agent", agent_id=42)

    other_agent_trace = await _seed_trace(session, start_offset=2)
    await _seed_span(session, other_agent_trace, span_type="agent", agent_id=7)

    # 非 agent span 即使 agent_id 相同也不命中。
    workflow_span_trace = await _seed_trace(session, start_offset=3)
    await _seed_span(
        session, workflow_span_trace, span_type="workflow", agent_id=42
    )

    no_span_trace = await _seed_trace(session, start_offset=4)
    assert no_span_trace not in _trace_ids(
        await _list_traces(client, viewer_token, agent_id=42)
    )

    page = await _list_traces(client, viewer_token, agent_id=42)
    assert _trace_ids(page) == {agent_trace}
    assert page["total"] == 1


@pytest.mark.asyncio
async def test_model_matches_model_name_or_provider(client, viewer_token, session):
    openai_trace = await _seed_trace(session, start_offset=1)
    openai_span = await _seed_span(session, openai_trace)
    await _seed_llm_call(
        session, openai_span, provider="openai", model_name="gpt-4o"
    )

    azure_trace = await _seed_trace(session, start_offset=2)
    azure_span = await _seed_span(session, azure_trace)
    await _seed_llm_call(
        session, azure_span, provider="azure", model_name="gpt-4o-mini"
    )

    await _seed_trace(session, start_offset=3)

    by_model = await _list_traces(client, viewer_token, model="gpt-4o")
    assert _trace_ids(by_model) == {openai_trace}

    by_provider = await _list_traces(client, viewer_token, model="azure")
    assert _trace_ids(by_provider) == {azure_trace}

    assert (await _list_traces(client, viewer_token, model="gpt-4"))["total"] == 0


@pytest.mark.asyncio
async def test_tool_matches_tool_id_or_tool_name(client, viewer_token, session):
    snapshot_trace = await _seed_trace(session, start_offset=1)
    snapshot_span = await _seed_span(session, snapshot_trace)
    await _seed_tool_call(
        session,
        snapshot_span,
        tool_id="camera.snapshot",
        tool_name="抓帧",
    )

    alarm_trace = await _seed_trace(session, start_offset=2)
    alarm_span = await _seed_span(session, alarm_trace)
    await _seed_tool_call(
        session, alarm_span, tool_id="alarm.raise", tool_name="告警"
    )

    by_tool_id = await _list_traces(client, viewer_token, tool="camera.snapshot")
    assert _trace_ids(by_tool_id) == {snapshot_trace}

    by_tool_name = await _list_traces(client, viewer_token, tool="告警")
    assert _trace_ids(by_tool_name) == {alarm_trace}


@pytest.mark.asyncio
async def test_failed_means_terminal_failure_statuses(client, viewer_token, session):
    succeeded = await _seed_trace(session, status="succeeded", start_offset=1)
    failed = await _seed_trace(session, status="failed", start_offset=2)
    timeout = await _seed_trace(session, status="timeout", start_offset=3)
    cancelled = await _seed_trace(session, status="cancelled", start_offset=4)
    running = await _seed_trace(session, status="running", start_offset=5)

    page = await _list_traces(client, viewer_token, failed="true")
    assert _trace_ids(page) == {failed, timeout, cancelled}
    assert page["total"] == 3

    # false / 缺省都不附加失败条件。
    assert _trace_ids(
        await _list_traces(client, viewer_token, failed="false")
    ) == {succeeded, failed, timeout, cancelled, running}
    assert _trace_ids(await _list_traces(client, viewer_token)) == {
        succeeded,
        failed,
        timeout,
        cancelled,
        running,
    }

    # 与显式 status 组合时按 AND 收窄。
    assert _trace_ids(
        await _list_traces(client, viewer_token, failed="true", status="timeout")
    ) == {timeout}


@pytest.mark.asyncio
async def test_slow_uses_3000ms_threshold(client, viewer_token, session):
    just_under = await _seed_trace(session, duration_ms=2999.9, start_offset=1)
    at_threshold = await _seed_trace(session, duration_ms=3000.0, start_offset=2)
    very_slow = await _seed_trace(session, duration_ms=86400.0, start_offset=3)
    missing = await _seed_trace(session, duration_ms=None, start_offset=4)

    page = await _list_traces(client, viewer_token, slow="true")
    assert _trace_ids(page) == {at_threshold, very_slow}
    assert page["total"] == 2

    assert _trace_ids(
        await _list_traces(client, viewer_token, slow="false")
    ) == {just_under, at_threshold, very_slow, missing}


@pytest.mark.asyncio
async def test_combination_and_pagination(client, viewer_token, session):
    first = await _seed_trace(
        session,
        source="workflow",
        status="failed",
        duration_ms=4000.0,
        start_offset=1,
    )
    second = await _seed_trace(
        session,
        source="workflow",
        status="timeout",
        duration_ms=5000.0,
        start_offset=2,
    )
    third = await _seed_trace(
        session,
        source="workflow",
        status="failed",
        duration_ms=6000.0,
        start_offset=3,
    )
    await _seed_trace(
        session,
        source="playground",
        status="failed",
        duration_ms=7000.0,
        start_offset=4,
    )
    await _seed_trace(
        session,
        source="workflow",
        status="failed",
        duration_ms=500.0,
        start_offset=5,
    )

    page1 = await _list_traces(
        client,
        viewer_token,
        source="workflow",
        failed="true",
        slow="true",
        page=1,
        size=2,
    )
    assert page1["total"] == 3
    assert page1["page"] == 1
    assert page1["size"] == 2
    assert [item["trace_id"] for item in page1["items"]] == [third, second]

    page2 = await _list_traces(
        client,
        viewer_token,
        source="workflow",
        failed="true",
        slow="true",
        page=2,
        size=2,
    )
    assert page2["total"] == 3
    assert [item["trace_id"] for item in page2["items"]] == [first]


@pytest.mark.asyncio
async def test_every_filter_is_tenant_scoped(
    client, viewer_token, other_tenant_token, session
):
    """同样的 source/user/agent/model/tool/status/duration 不得跨租户命中。"""

    async def seed(tenant_id):
        trace_id = await _seed_trace(
            session,
            tenant_id=tenant_id,
            source="shared-source",
            user_id="shared-user",
            status="failed",
            duration_ms=5000.0,
        )
        agent_span = await _seed_span(
            session, trace_id, tenant_id=tenant_id, span_type="agent", agent_id=99
        )
        await _seed_span(
            session, trace_id, tenant_id=tenant_id, span_type="tool", tool_id="t1"
        )
        await _seed_llm_call(
            session,
            agent_span,
            tenant_id=tenant_id,
            provider="shared-provider",
            model_name="shared-model",
        )
        await _seed_tool_call(
            session,
            agent_span,
            tenant_id=tenant_id,
            tool_id="shared-tool",
            tool_name="共享工具",
        )
        return trace_id

    ours = await seed(TENANT)
    theirs = await seed(OTHER_TENANT)

    filters = [
        {"source": "shared-source"},
        {"user_id": "shared-user"},
        {"agent_id": 99},
        {"model": "shared-model"},
        {"model": "shared-provider"},
        {"tool": "shared-tool"},
        {"tool": "共享工具"},
        {"failed": "true"},
        {"slow": "true"},
    ]
    for query in filters:
        page = await _list_traces(client, viewer_token, **query)
        assert _trace_ids(page) == {ours}, query

    other_page = await _list_traces(
        client, other_tenant_token, source="shared-source"
    )
    assert _trace_ids(other_page) == {theirs}
