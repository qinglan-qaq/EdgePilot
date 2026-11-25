"""0003_obs_llm_tool_prompt: 观测明细表 llm_calls / tool_calls

字段权威来源：可观测设计 §5.2 / §5.3。
- 全部含 id bigint identity PK、tenant_id uuid not null、created_at timestamptz not null；
  两张表都有 updated_at。
- 不设跨表外键（保留期独立清理，引用完整性由应用层保证），span_id 由写入端注入。
- id 使用 BigInteger + Identity()，与 0001/0002 一致。
- 索引在 §5.3 基础上补 ix_<table>_tenant_id 与 ix_<table>_created_at：
  前者对齐 ORM TenantMixin(index=True)，后者供保留期清理使用。

Task 7 会在本文件追加 prompt_templates / prompt_versions / prompt_executions
三张表（同一 revision），本 Task 先只建 llm_calls / tool_calls。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_obs_llm_tool_prompt"
down_revision = "0002_obs_base"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    ]


def upgrade() -> None:
    # ---- llm_calls -------------------------------------------------------
    op.create_table(
        "llm_calls",
        _id(),
        _tenant(),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_name", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(64)),
        sa.Column("temperature", sa.Float),
        sa.Column("top_p", sa.Float),
        sa.Column("max_tokens", sa.Integer),
        sa.Column("tool_definitions", JSONB),
        sa.Column("tool_choice", sa.String(32)),
        sa.Column("request_messages", JSONB),
        sa.Column("raw_response", JSONB),
        sa.Column("structured_response", JSONB),
        sa.Column("response_format", sa.String(32)),
        sa.Column("finish_reason", sa.String(32)),
        sa.Column("prompt_tokens", sa.Integer),
        sa.Column("completion_tokens", sa.Integer),
        sa.Column("cached_tokens", sa.Integer),
        sa.Column("total_tokens", sa.Integer),
        sa.Column("cost", sa.Numeric(12, 6)),
        sa.Column("request_id", sa.String(128)),
        sa.Column("error", JSONB),
        *_timestamps(),
    )
    op.create_index("ix_llm_calls_tenant_id", "llm_calls", ["tenant_id"])
    op.create_index("ix_llm_calls_created_at", "llm_calls", ["created_at"])
    op.create_index("ix_llm_calls_span_attempt", "llm_calls", ["span_id", "attempt"])
    op.create_index(
        "ix_llm_calls_tenant_model_created",
        "llm_calls",
        ["tenant_id", "model_name", "created_at"],
    )

    # ---- tool_calls ------------------------------------------------------
    op.create_table(
        "tool_calls",
        _id(),
        _tenant(),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
        sa.Column("tool_id", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("tool_version", sa.String(32)),
        sa.Column("description", sa.Text),
        sa.Column("input_schema", JSONB),
        sa.Column("output_schema", JSONB),
        sa.Column("input_arguments", JSONB),
        sa.Column("output_result", JSONB),
        sa.Column("execution_status", sa.String(24), nullable=False),
        sa.Column("permission_result", sa.String(24)),
        sa.Column("duration_ms", sa.Float),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", JSONB),
        *_timestamps(),
    )
    op.create_index("ix_tool_calls_tenant_id", "tool_calls", ["tenant_id"])
    op.create_index("ix_tool_calls_created_at", "tool_calls", ["created_at"])
    op.create_index("ix_tool_calls_span_attempt", "tool_calls", ["span_id", "attempt"])
    op.create_index(
        "ix_tool_calls_tenant_tool_created",
        "tool_calls",
        ["tenant_id", "tool_id", "created_at"],
    )
    op.create_index(
        "ix_tool_calls_tenant_status_created",
        "tool_calls",
        ["tenant_id", "execution_status", "created_at"],
    )

    # Task 7 追加: prompt_templates / prompt_versions / prompt_executions


def downgrade() -> None:
    op.drop_index("ix_tool_calls_tenant_status_created", table_name="tool_calls")
    op.drop_index("ix_tool_calls_tenant_tool_created", table_name="tool_calls")
    op.drop_index("ix_tool_calls_span_attempt", table_name="tool_calls")
    op.drop_index("ix_tool_calls_created_at", table_name="tool_calls")
    op.drop_index("ix_tool_calls_tenant_id", table_name="tool_calls")
    op.drop_table("tool_calls")

    op.drop_index("ix_llm_calls_tenant_model_created", table_name="llm_calls")
    op.drop_index("ix_llm_calls_span_attempt", table_name="llm_calls")
    op.drop_index("ix_llm_calls_created_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_tenant_id", table_name="llm_calls")
    op.drop_table("llm_calls")
