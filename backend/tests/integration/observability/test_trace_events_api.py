"""Trace 事件 / 扁平 Span 列表 / Prompt 执行记录 查询契约（Task 16）。

全部写入直接走 SQL 且不提交，与 seeded_trace 共用外层事务；
这里不使用 rollback()，避免破坏夹具。
"""

import json

import pytest
from sqlalchemy import text

from tests.conftest import TENANT, auth_headers
from tests.integration.observability.conftest import AGENT_ID, ROOT_ID

TRACE_URL = "/api/v1/observability/traces"
SPAN_URL = "/api/v1/observability/spans"

# 仅供“无事件 / 无 Prompt 执行”用例使用，避免依赖 seeded_trace 的事件数据。
EMPTY_TRACE_ID = "e" * 32
EMPTY_SPAN_ID = "a" * 16


async def _seed_events(session, trace_id: str) -> None:
    """故意乱序插入，验证接口按 sequence_number 升序返回。"""
    rows = [
        (3, "retry", AGENT_ID),
        (1, "start", None),
        (2, "llm_call", AGENT_ID),
    ]
    for sequence_number, event_type, span_id in rows:
        await session.execute(
            text(
                "INSERT INTO trace_events (trace_id, tenant_id, span_id, event_type,"
                " event_data, sequence_number, created_at)"
                " VALUES (:t, :ten, :s, :ty, CAST(:d AS jsonb), :seq, now())"
            ),
            {
                "t": trace_id,
                "ten": TENANT,
                "s": span_id,
                "ty": event_type,
                "d": json.dumps({"seq": sequence_number}),
                "seq": sequence_number,
            },
        )


async def _seed_extra_span(session, trace_id: str) -> str:
    """插入一个开始时间早于 seeded_trace 两个 span 的 tool span。"""
    span_id = "d" * 16
    await session.execute(
        text(
            "INSERT INTO spans (span_id, tenant_id, trace_id, parent_span_id, name,"
            " span_type, status, start_time)"
            " VALUES (:s, :ten, :t, :p, 'tool.ping', 'tool', 'succeeded',"
            " now() - interval '1 second')"
        ),
        {"s": span_id, "ten": TENANT, "t": trace_id, "p": AGENT_ID},
    )
    return span_id


async def _seed_empty_trace(session) -> None:
    await session.execute(
        text(
            "INSERT INTO traces (trace_id, tenant_id, name, source, status,"
            " total_spans, start_time) VALUES (:t, :ten, 'empty', 'test',"
            " 'succeeded', 1, now())"
        ),
        {"t": EMPTY_TRACE_ID, "ten": TENANT},
    )
    await session.execute(
        text(
            "INSERT INTO spans (span_id, tenant_id, trace_id, name, span_type,"
            " status, start_time) VALUES (:s, :ten, :t, 'agent.empty', 'agent',"
            " 'succeeded', now())"
        ),
        {"s": EMPTY_SPAN_ID, "ten": TENANT, "t": EMPTY_TRACE_ID},
    )


@pytest.mark.asyncio
async def test_events_are_sorted_by_sequence_number(
    client, viewer_token, seeded_trace, session
):
    await _seed_events(session, seeded_trace)
    # 同租户但不同 trace 的事件不得混入响应。
    await session.execute(
        text(
            "INSERT INTO trace_events (trace_id, tenant_id, event_type, sequence_number)"
            " VALUES (:t, :ten, 'other_trace', 1)"
        ),
        {"t": EMPTY_TRACE_ID, "ten": TENANT},
    )

    r = await client.get(
        f"{TRACE_URL}/{seeded_trace}/events", headers=auth_headers(viewer_token)
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert [event["sequence_number"] for event in data] == [1, 2, 3]
    assert [event["event_type"] for event in data] == ["start", "llm_call", "retry"]
    assert data[0]["span_id"] is None
    assert data[1]["span_id"] == AGENT_ID
    assert data[1]["event_data"] == {"seq": 2}
    assert set(data[0]) == {
        "id",
        "trace_id",
        "span_id",
        "event_type",
        "event_data",
        "sequence_number",
        "created_at",
    }


@pytest.mark.asyncio
async def test_flat_spans_sorted_by_start_time_and_id(
    client, viewer_token, seeded_trace, session
):
    extra_span_id = await _seed_extra_span(session, seeded_trace)

    r = await client.get(
        f"{TRACE_URL}/{seeded_trace}/spans", headers=auth_headers(viewer_token)
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 3
    # 最早开始的 tool span 排最前，其余两个同一时刻按 id 升序。
    assert data[0]["span_id"] == extra_span_id
    assert data[1]["span_id"] == ROOT_ID
    assert data[2]["span_id"] == AGENT_ID
    assert data[2]["parent_span_id"] == ROOT_ID
    assert "children" not in data[0]


@pytest.mark.asyncio
async def test_span_detail_includes_prompt_executions(
    client, viewer_token, seeded_trace, session
):
    await session.execute(
        text(
            "INSERT INTO prompt_executions (tenant_id, trace_id, span_id,"
            " template_id, template_version, variables, rendered_messages,"
            " prompt_hash, render_status, model_name)"
            " VALUES (:ten, :t, :s, 7, 3, CAST(:v AS jsonb), CAST(:m AS jsonb),"
            " :h, 'ok', 'mock-model')"
        ),
        {
            "ten": TENANT,
            "t": seeded_trace,
            "s": AGENT_ID,
            "v": json.dumps({"question": "你好", "locale": "zh"}),
            "m": json.dumps(
                [
                    {"role": "system", "content": "S"},
                    {"role": "user", "content": "你好"},
                ],
                ensure_ascii=False,
            ),
            "h": "f" * 64,
        },
    )

    r = await client.get(f"{SPAN_URL}/{AGENT_ID}", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data["prompt_executions"]) == 1
    execution = data["prompt_executions"][0]
    assert execution["template_id"] == 7
    assert execution["template_version"] == 3
    assert execution["render_status"] == "ok"
    assert execution["model_name"] == "mock-model"
    assert execution["prompt_hash"] == "f" * 64
    assert execution["variables"] == {"question": "你好", "locale": "zh"}
    assert execution["rendered_messages"][1]["content"] == "你好"


@pytest.mark.asyncio
async def test_events_and_spans_cross_tenant_are_404(
    client, other_tenant_token, seeded_trace
):
    headers = auth_headers(other_tenant_token)

    events = await client.get(f"{TRACE_URL}/{seeded_trace}/events", headers=headers)
    assert events.status_code == 404

    spans = await client.get(f"{TRACE_URL}/{seeded_trace}/spans", headers=headers)
    assert spans.status_code == 404


@pytest.mark.asyncio
async def test_empty_events_and_prompt_executions(
    client, viewer_token, session
):
    await _seed_empty_trace(session)

    events = await client.get(
        f"{TRACE_URL}/{EMPTY_TRACE_ID}/events", headers=auth_headers(viewer_token)
    )
    assert events.status_code == 200
    assert events.json()["data"] == []

    spans = await client.get(
        f"{TRACE_URL}/{EMPTY_TRACE_ID}/spans", headers=auth_headers(viewer_token)
    )
    assert spans.status_code == 200
    assert len(spans.json()["data"]) == 1

    detail = await client.get(
        f"{SPAN_URL}/{EMPTY_SPAN_ID}", headers=auth_headers(viewer_token)
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["prompt_executions"] == []


@pytest.mark.asyncio
async def test_events_unknown_trace_is_404(client, viewer_token, seeded_trace):
    r = await client.get(
        f"{TRACE_URL}/{'0' * 32}/events", headers=auth_headers(viewer_token)
    )
    assert r.status_code == 404

    spans = await client.get(
        f"{TRACE_URL}/{'0' * 32}/spans", headers=auth_headers(viewer_token)
    )
    assert spans.status_code == 404
