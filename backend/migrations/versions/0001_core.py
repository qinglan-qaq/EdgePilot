"""0001_core: 16 张业务表（含观测前置字段）

字段权威来源：架构稿 §2 ER 表，并按可观测设计 §5.1 做增删：
- workflow_runs + root_span_id
- workflow_node_runs + span_id
- agent_runs - token_usage/- tool_calls，+ span_id/total_tokens/total_cost
- agents - prompt_system，+ prompt_template_id/prompt_version
- tools + version/dry_run_supported
- model_configs + 三档 Number(10,6) 单价

全库不建外键：ER 中的 FK 仅表示逻辑引用；业务表之间一律不加约束。
所有 id 用 BigInteger + Identity()（PG 上 bigint 不会自动建序列）。
唯一约束带 tenant_id（agent_versions / agent_tool_bindings /
workflow_versions / knowledge_chunks 的四条按 ER 原样，已由 tenant 间接限定）。
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
EMPTY_JSON = sa.text("'{}'::jsonb")


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def _created() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now())


def _updated() -> sa.Column:
    return sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now())


def _tenant_index(table: str) -> None:
    """TenantMixin(tenant_id, index=True) 对应的单列索引。"""
    op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ---- users ---------------------------------------------------------
    op.create_table(
        "users",
        _id(),
        _tenant(),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    _tenant_index("users")

    # ---- model_configs -------------------------------------------------
    op.create_table(
        "model_configs",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("model_type", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("base_url", sa.String(512)),
        sa.Column("api_key_enc", sa.Text),
        sa.Column("params", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("deployment_location", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("input_price_per_1k", sa.Numeric(10, 6)),
        sa.Column("output_price_per_1k", sa.Numeric(10, 6)),
        sa.Column("cached_price_per_1k", sa.Numeric(10, 6)),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "name", name="uq_model_configs_tenant_name"),
    )
    _tenant_index("model_configs")

    # ---- tools ---------------------------------------------------------
    op.create_table(
        "tools",
        _id(),
        _tenant(),
        sa.Column("tool_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("source", sa.String(16), nullable=False, server_default="local"),
        sa.Column("input_schema", JSONB),
        sa.Column("output_schema", JSONB),
        sa.Column("permission_scope", sa.String(64)),
        sa.Column("timeout_ms", sa.Integer, nullable=False, server_default="30000"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("side_effect", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("handler", sa.String(255)),
        sa.Column("version", sa.String(32), nullable=False, server_default="1.0.0"),
        sa.Column("dry_run_supported", sa.Boolean, nullable=False, server_default=sa.text("false")),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "tool_id", name="uq_tools_tenant_tool_id"),
    )
    _tenant_index("tools")

    # ---- agents --------------------------------------------------------
    op.create_table(
        "agents",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("agent_type", sa.String(32), nullable=False),
        sa.Column("model_config_id", sa.BigInteger),
        sa.Column("prompt_template_id", sa.BigInteger),
        sa.Column("prompt_version", sa.Integer),
        sa.Column("config", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="0"),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
    )
    _tenant_index("agents")
    op.create_index("ix_agents_tenant_type", "agents", ["tenant_id", "agent_type"])

    # ---- agent_versions -------------------------------------------------
    op.create_table(
        "agent_versions",
        _id(),
        _tenant(),
        sa.Column("agent_id", sa.BigInteger, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("snapshot", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("created_by", sa.BigInteger),
        _created(),
        _updated(),
        sa.UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
    )
    _tenant_index("agent_versions")

    # ---- agent_tool_bindings --------------------------------------------
    op.create_table(
        "agent_tool_bindings",
        _id(),
        _tenant(),
        sa.Column("agent_id", sa.BigInteger, nullable=False),
        sa.Column("tool_id", sa.BigInteger, nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        _created(),
        _updated(),
        sa.UniqueConstraint("agent_id", "tool_id", name="uq_agent_tool_bindings_agent_tool"),
    )
    _tenant_index("agent_tool_bindings")

    # ---- workflows ------------------------------------------------------
    op.create_table(
        "workflows",
        _id(),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_by", sa.BigInteger),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "name", name="uq_workflows_tenant_name"),
    )
    _tenant_index("workflows")

    # ---- workflow_versions ----------------------------------------------
    op.create_table(
        "workflow_versions",
        _id(),
        _tenant(),
        sa.Column("workflow_id", sa.BigInteger, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("dsl", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("dsl_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.BigInteger),
        _created(),
        _updated(),
        sa.UniqueConstraint("workflow_id", "version", name="uq_workflow_versions_workflow_version"),
    )
    _tenant_index("workflow_versions")

    # ---- workflow_runs --------------------------------------------------
    op.create_table(
        "workflow_runs",
        _id(),
        _tenant(),
        sa.Column("workflow_version_id", sa.BigInteger, nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("trigger_payload", JSONB),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("input", JSONB),
        sa.Column("output", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("root_span_id", sa.String(16)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _created(),
        _updated(),
    )
    _tenant_index("workflow_runs")
    op.create_index(
        "ix_workflow_runs_tenant_status_created",
        "workflow_runs",
        ["tenant_id", "status", sa.text("created_at DESC")],
    )
    op.create_index("ix_workflow_runs_trace_id", "workflow_runs", ["trace_id"])

    # ---- workflow_node_runs ---------------------------------------------
    op.create_table(
        "workflow_node_runs",
        _id(),
        _tenant(),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("input", JSONB),
        sa.Column("output", JSONB),
        sa.Column("approval", JSONB),
        sa.Column("error", sa.Text),
        sa.Column("span_id", sa.String(16)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _created(),
        _updated(),
        sa.UniqueConstraint("run_id", "node_id", name="uq_workflow_node_runs_run_node"),
    )
    _tenant_index("workflow_node_runs")
    op.create_index(
        "ix_workflow_node_runs_waiting_approval",
        "workflow_node_runs",
        ["status"],
        postgresql_where=sa.text("status = 'waiting_approval'"),
    )

    # ---- edge_nodes -----------------------------------------------------
    op.create_table(
        "edge_nodes",
        _id(),
        _tenant(),
        sa.Column("node_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("os", sa.String(32)),
        sa.Column("arch", sa.String(32)),
        sa.Column("agent_version", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False, server_default="offline"),
        sa.Column("ip", sa.String(64)),
        sa.Column("labels", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("metrics", JSONB),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "node_key", name="uq_edge_nodes_tenant_node_key"),
    )
    _tenant_index("edge_nodes")

    # ---- devices --------------------------------------------------------
    op.create_table(
        "devices",
        _id(),
        _tenant(),
        sa.Column("node_id", sa.BigInteger, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="camera"),
        sa.Column("config", JSONB, nullable=False, server_default=EMPTY_JSON),
        sa.Column("status", sa.String(16), nullable=False, server_default="offline"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        _created(),
        _updated(),
    )
    _tenant_index("devices")

    # ---- vision_events --------------------------------------------------
    op.create_table(
        "vision_events",
        _id(),
        _tenant(),
        sa.Column("node_id", sa.BigInteger),
        sa.Column("device_id", sa.BigInteger),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("confidence", sa.Numeric),
        sa.Column("track_id", sa.String(64)),
        sa.Column("snapshot_path", sa.String(512)),
        sa.Column("bbox", JSONB),
        sa.Column("model_version", sa.String(32)),
        sa.Column("payload", JSONB),
        sa.Column("dedup_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        _created(),
        _updated(),
        sa.UniqueConstraint("tenant_id", "dedup_key", name="uq_vision_events_tenant_dedup"),
    )
    _tenant_index("vision_events")
    op.create_index(
        "ix_vision_events_tenant_occurred",
        "vision_events",
        ["tenant_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "ix_vision_events_device_occurred",
        "vision_events",
        ["device_id", sa.text("occurred_at DESC")],
    )

    # ---- agent_runs -----------------------------------------------------
    op.create_table(
        "agent_runs",
        _id(),
        _tenant(),
        sa.Column("workflow_run_id", sa.BigInteger),
        sa.Column("node_run_id", sa.BigInteger),
        sa.Column("agent_id", sa.BigInteger, nullable=False),
        sa.Column("agent_version", sa.Integer),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input", JSONB),
        sa.Column("output", JSONB),
        sa.Column("span_id", sa.String(16)),
        sa.Column("total_tokens", sa.Integer),
        sa.Column("total_cost", sa.Numeric(12, 6)),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _created(),
        _updated(),
    )
    _tenant_index("agent_runs")
    op.create_index(
        "ix_agent_runs_agent_created",
        "agent_runs",
        ["agent_id", sa.text("created_at DESC")],
    )

    # ---- knowledge_chunks -----------------------------------------------
    op.create_table(
        "knowledge_chunks",
        _id(),
        _tenant(),
        sa.Column("doc_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255)),
        sa.Column("source", sa.String(255)),
        sa.Column("chunk_no", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column("metadata", JSONB, nullable=False, server_default=EMPTY_JSON),
        _created(),
        sa.UniqueConstraint("doc_id", "chunk_no", name="uq_knowledge_chunks_doc_chunk"),
    )
    _tenant_index("knowledge_chunks")
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw",
        "knowledge_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index(
        "ix_knowledge_chunks_content_tsv",
        "knowledge_chunks",
        [sa.text("to_tsvector('simple', content)")],
        postgresql_using="gin",
    )

    # ---- audit_logs -----------------------------------------------------
    op.create_table(
        "audit_logs",
        _id(),
        _tenant(),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.BigInteger),
        sa.Column("ip", sa.String(64)),
        sa.Column("user_agent", sa.String(255)),
        sa.Column("detail", JSONB),
        sa.Column("trace_id", sa.String(32)),
        _created(),
    )
    _tenant_index("audit_logs")
    op.create_index(
        "ix_audit_logs_tenant_created",
        "audit_logs",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_audit_logs_resource",
        "audit_logs",
        ["resource_type", "resource_id"],
    )


def downgrade() -> None:
    for table in (
        "audit_logs",
        "knowledge_chunks",
        "agent_runs",
        "vision_events",
        "devices",
        "edge_nodes",
        "workflow_node_runs",
        "workflow_runs",
        "workflow_versions",
        "workflows",
        "agent_tool_bindings",
        "agent_versions",
        "agents",
        "tools",
        "model_configs",
        "users",
    ):
        op.drop_table(table)
