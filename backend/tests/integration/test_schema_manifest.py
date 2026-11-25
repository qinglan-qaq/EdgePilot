import pytest
from sqlalchemy import text

EXPECTED_TABLES = {
    "users", "agents", "agent_versions", "model_configs", "tools", "agent_tool_bindings",
    "workflows", "workflow_versions", "workflow_runs", "workflow_node_runs",
    "edge_nodes", "devices", "vision_events", "agent_runs", "knowledge_chunks", "audit_logs",
}
EXPECTED_COLUMNS = {
    "workflow_runs": {"root_span_id"},
    "workflow_node_runs": {"span_id"},
    "agent_runs": {"span_id", "total_tokens", "total_cost"},
    "agents": {"prompt_template_id", "prompt_version"},
    "tools": {"version", "dry_run_supported"},
    "model_configs": {"input_price_per_1k", "output_price_per_1k", "cached_price_per_1k"},
}
REMOVED_COLUMNS = {
    "agents": {"prompt_system"},
    "agent_runs": {"token_usage", "tool_calls"},
}
TENANT_TABLES = {"users", "agents", "agent_versions", "model_configs", "tools",
                 "agent_tool_bindings", "workflows", "workflow_versions", "workflow_runs",
                 "workflow_node_runs", "edge_nodes", "devices", "vision_events",
                 "agent_runs", "knowledge_chunks", "audit_logs"}


@pytest.mark.asyncio
async def test_all_business_tables_exist(session):
    rows = await session.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ))
    assert EXPECTED_TABLES <= {r[0] for r in rows}


@pytest.mark.asyncio
async def test_added_and_removed_columns(session):
    for table, columns in EXPECTED_COLUMNS.items():
        rows = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": table})
        actual = {r[0] for r in rows}
        assert columns <= actual, f"{table} 缺少 {columns - actual}"
    for table, columns in REMOVED_COLUMNS.items():
        rows = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": table})
        assert not (columns & {r[0] for r in rows}), f"{table} 仍存在 {columns}"


@pytest.mark.asyncio
async def test_every_business_table_is_tenant_scoped(session):
    for table in TENANT_TABLES:
        rows = await session.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name='tenant_id'"
        ), {"t": table})
        row = rows.first()
        assert row is not None, f"{table} 缺少 tenant_id"
        assert row[0] == "NO", f"{table}.tenant_id 必须 NOT NULL"
