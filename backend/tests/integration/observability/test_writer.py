import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.collector import EventCollector
from app.observability.records import LLMCallRecord, ToolCallRecord
from app.observability.tracer import Tracer
from app.observability.writer import BatchWriter

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@contextmanager
def tenant(tid=TENANT):
    token = set_tenant_id(tid)
    try:
        yield tid
    finally:
        reset_tenant_id(token)


def make_tracer(writer):
    return Tracer(enabled=True, sample_rate=1.0,
                  collector=EventCollector(maxsize=100, batch_size=100,
                                           flush_ms=100, writer=writer))


@pytest.mark.asyncio
async def test_closed_span_persists_llm_call_and_cost_unavailable(session, session_factory):
    writer = BatchWriter(session_factory)
    t = make_tracer(writer)

    with tenant():
        async with t.start_trace("demo") as trace:
            trace.set_output({"ok": True})
            async with t.start_span("llm.x", "llm") as span:
                span.record_llm_call(LLMCallRecord(provider="mock", model_name="m1",
                                                   total_tokens=10, cost=None))
        trace_id = trace.trace_id
        # 必须在两个 CM 之外取记录，否则拿不到 trace.close，汇总列会是空的
        await writer.write(t.collector.pending())

    row = (await session.execute(
        text("SELECT total_tokens, total_cost, cost_unavailable_count "
             "FROM traces WHERE trace_id=:t"), {"t": trace_id})).one()
    assert row.total_tokens == 10
    assert row.total_cost is None
    assert row.cost_unavailable_count == 1

    output_ok = await session.scalar(text(
        "SELECT jsonb_extract_path_text(output, 'ok') FROM traces WHERE trace_id=:t"),
        {"t": trace_id})
    assert output_ok == "true"

    span_status = await session.scalar(
        text("SELECT status FROM spans WHERE trace_id=:t AND span_type='llm'"), {"t": trace_id})
    assert span_status == "succeeded"


@pytest.mark.asyncio
async def test_tool_call_rows_are_one_to_many_per_span(session, session_factory):
    writer = BatchWriter(session_factory)
    t = make_tracer(writer)

    with tenant():
        async with t.start_trace("demo"):
            async with t.start_span("tool.q", "tool", tool_id="q") as span:
                span.record_tool_call(ToolCallRecord("q", "query", execution_status="failed"))
                span.record_tool_call(ToolCallRecord("q", "query", execution_status="succeeded",
                                                     retry_count=1))
        await writer.write(t.collector.pending())

    count = await session.scalar(text("SELECT count(*) FROM tool_calls WHERE tool_id='q'"))
    assert count == 2
