import json
import uuid

import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.sse import format_event, stream_trace_events


def test_format_event_uses_sequence_as_id():
    chunk = format_event(7, {"event_type": "tool_started", "event_data": {"tool": "q"}})
    assert chunk.startswith("id: 7\n")
    assert chunk.endswith("\n\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload["event_type"] == "tool_started"


@pytest.mark.asyncio
async def test_stream_replays_events_after_last_event_id(session, session_factory, fake_pubsub):
    tid = uuid.uuid4()
    token = set_tenant_id(tid)
    try:
        await session.execute(text("""
            INSERT INTO traces (tenant_id, trace_id, name, source, status, start_time)
            VALUES (:t, 'trace-a', 'demo', 'workflow_run', 'running', now())
        """), {"t": tid})
        for seq, etype in ((1, "agent_started"), (2, "tool_started")):
            await session.execute(text("""
                INSERT INTO trace_events (tenant_id, trace_id, span_id, event_type,
                                          event_data, sequence_number, created_at)
                VALUES (:t, 'trace-a', NULL, :e, '{}'::jsonb, :s, now())
            """), {"t": tid, "e": etype, "s": seq})
        await session.commit()

        gen = stream_trace_events(session_factory, fake_pubsub, "trace-a", last_event_id=1)
        first = await gen.__anext__()
        await gen.aclose()
    finally:
        reset_tenant_id(token)

    assert "id: 2" in first      # 只补发 Last-Event-ID 之后的事件
    assert "tool_started" in first
    assert "agent_started" not in first
