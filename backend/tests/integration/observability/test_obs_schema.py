import pytest
from sqlalchemy import text

TABLES = {"traces", "spans", "trace_events"}
REQUIRED = {
    "traces": {"trace_id", "tenant_id", "name", "source", "user_id", "session_id",
               "root_span_id", "status", "input", "output", "start_time", "end_time",
               "duration_ms", "total_tokens", "total_cost", "cost_unavailable_count",
               "total_spans", "error_count", "metadata", "created_at"},
    "spans": {"span_id", "tenant_id", "trace_id", "parent_span_id", "name", "span_type",
              "agent_id", "agent_version", "workflow_id", "node_id", "tool_id", "status",
              "attempt", "start_time", "end_time", "duration_ms", "input", "output",
              "error", "retry_count", "metadata", "links", "created_at"},
    "trace_events": {"tenant_id", "trace_id", "span_id", "event_type", "event_data",
                     "sequence_number", "created_at"},
}


@pytest.mark.asyncio
async def test_obs_tables_and_columns(session):
    for table, cols in REQUIRED.items():
        rows = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"), {"t": table})
        actual = {r[0] for r in rows}
        assert cols <= actual, f"{table} 缺少 {cols - actual}"


@pytest.mark.asyncio
async def test_obs_tables_are_tenant_scoped_and_have_no_business_fk(session):
    rows = await session.execute(text("""
        SELECT c.table_name, c.column_name FROM information_schema.columns c
        WHERE c.table_schema='public' AND c.column_name='tenant_id'
    """))
    assert TABLES <= {r[0] for r in rows}

    fks = await session.execute(text("""
        SELECT tc.table_name FROM information_schema.table_constraints tc
        WHERE tc.table_schema='public' AND tc.constraint_type='FOREIGN KEY'
          AND tc.table_name = ANY(:tables)
    """), {"tables": list(TABLES)})
    assert list(fks) == [], "观测表不得有外键"


@pytest.mark.asyncio
async def test_trace_events_sequence_is_unique_per_trace(session):
    ddl = await session.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname='public' AND tablename='trace_events'
    """))
    assert any("unique" in (row[0] or "").lower() and "sequence_number" in row[0] for row in ddl)
