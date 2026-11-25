"""0002_obs_base: 观测基础表 traces / spans / trace_events

字段权威来源：可观测设计 §5.2 / §5.3。
- 全部含 id bigint identity PK、tenant_id uuid not null、created_at timestamptz；
  traces / spans 另有 updated_at，trace_events 只追加无 updated_at。
- 三张表之间与业务表之间一律不建外键（保留期独立清理，引用完整性由应用层保证）。
- id 使用 BigInteger + Identity()，与 0001_core 一致（PG 上 bigint 不自建序列）。
- tenant_id 单列索引对应 ORM TenantMixin(index=True)。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002_obs_base"
down_revision = "0001_core"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB
EMPTY_JSON = sa.text("'{}'::jsonb")


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def upgrade() -> None:
    # ---- traces ---------------------------------------------------------
    op.create_table(
        "traces",
        _id(),
        _tenant(),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(64)),
        sa.Column("session_id", sa.String(64)),
        sa.Column("root_span_id", sa.String(16)),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("input", JSONB),
        sa.Column("output", JSONB),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Double),
        sa.Column("total_tokens", sa.BigInteger),
        sa.Column("total_cost", sa.Numeric(12, 6)),
        sa.Column("cost_unavailable_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("total_spans", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("metadata", JSONB, server_default=EMPTY_JSON),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_traces_tenant_id", "traces", ["tenant_id"])
    op.create_index("uq_traces_trace_id", "traces", ["trace_id"], unique=True)
    op.create_index("ix_traces_tenant_start", "traces", ["tenant_id", "start_time"])
    op.create_index("ix_traces_tenant_status_start", "traces", ["tenant_id", "status", "start_time"])
    op.create_index("ix_traces_metadata_gin", "traces", ["metadata"], postgresql_using="gin")

    # ---- spans ----------------------------------------------------------
    op.create_table(
        "spans",
        _id(),
        _tenant(),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("parent_span_id", sa.String(16)),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("span_type", sa.String(24), nullable=False),
        sa.Column("agent_id", sa.BigInteger),
        sa.Column("agent_version", sa.Integer),
        sa.Column("workflow_id", sa.BigInteger),
        sa.Column("node_id", sa.String(64)),
        sa.Column("tool_id", sa.String(64)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_time", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Double),
        sa.Column("input", JSONB),
        sa.Column("output", JSONB),
        sa.Column("error", JSONB),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("metadata", JSONB, server_default=EMPTY_JSON),
        sa.Column("links", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_spans_tenant_id", "spans", ["tenant_id"])
    op.create_index("uq_spans_span_id", "spans", ["span_id"], unique=True)
    op.create_index("ix_spans_trace_start", "spans", ["trace_id", "start_time"])
    op.create_index("ix_spans_tenant_type_start", "spans", ["tenant_id", "span_type", "start_time"])
    op.create_index("ix_spans_tenant_agent_start", "spans", ["tenant_id", "agent_id", "start_time"])
    op.create_index("ix_spans_tenant_tool_start", "spans", ["tenant_id", "tool_id", "start_time"])

    # ---- trace_events ---------------------------------------------------
    op.create_table(
        "trace_events",
        _id(),
        _tenant(),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("span_id", sa.String(16)),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("event_data", JSONB),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_trace_events_tenant_id", "trace_events", ["tenant_id"])
    op.create_index(
        "uq_events_trace_seq", "trace_events", ["trace_id", "sequence_number"], unique=True
    )
    op.create_index("ix_events_trace_created", "trace_events", ["trace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_events_trace_created", table_name="trace_events")
    op.drop_index("uq_events_trace_seq", table_name="trace_events")
    op.drop_index("ix_trace_events_tenant_id", table_name="trace_events")
    op.drop_table("trace_events")

    op.drop_index("ix_spans_tenant_tool_start", table_name="spans")
    op.drop_index("ix_spans_tenant_agent_start", table_name="spans")
    op.drop_index("ix_spans_tenant_type_start", table_name="spans")
    op.drop_index("ix_spans_trace_start", table_name="spans")
    op.drop_index("uq_spans_span_id", table_name="spans")
    op.drop_index("ix_spans_tenant_id", table_name="spans")
    op.drop_table("spans")

    op.drop_index("ix_traces_metadata_gin", table_name="traces")
    op.drop_index("ix_traces_tenant_status_start", table_name="traces")
    op.drop_index("ix_traces_tenant_start", table_name="traces")
    op.drop_index("uq_traces_trace_id", table_name="traces")
    op.drop_index("ix_traces_tenant_id", table_name="traces")
    op.drop_table("traces")
