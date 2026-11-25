"""0004_obs_retrieval: pg_trgm 索引 + retrieval_records 表

字段权威来源：可观测设计 §5.4 / 平台 Task 20。

- ``CREATE EXTENSION IF NOT EXISTS pg_trgm``；
- ``knowledge_chunks.content`` 增加 GIN trigram 索引，供 ``word_similarity`` 兜底；
- ``retrieval_records`` 只追加、无 updated_at，不建跨表外键；
- 表结构与 ``app/observability/models/retrieval.py`` 及
  ``app/observability/writer.py`` 的 ``_RETRIEVAL_COLUMNS`` 完全对齐（writer 不写
  attempt 列，本表也不建 attempt）。

downgrade 只删除表和 trigram 索引，不 drop pg_trgm 扩展（其它对象可能仍在使用）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_obs_retrieval"
down_revision = "0003_obs_llm_tool_prompt"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_index(
        "ix_knowledge_chunks_content_trgm",
        "knowledge_chunks",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )

    op.create_table(
        "retrieval_records",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("span_id", sa.String(16), nullable=False),
        sa.Column("query", sa.Text, nullable=False),
        sa.Column("rewritten_query", sa.Text),
        sa.Column("retrieval_strategy", sa.String(32)),
        sa.Column("embedding_model", sa.String(128)),
        sa.Column("vector_store", sa.String(64)),
        sa.Column("reranker_model", sa.String(128)),
        sa.Column("top_k", sa.Integer),
        sa.Column("similarity_threshold", sa.Double),
        sa.Column(
            "retrieved_documents", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "reranked_documents", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("final_context", sa.Text),
        sa.Column("duration_ms", sa.Double),
        sa.Column("error", JSONB),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_retrieval_records_tenant_id", "retrieval_records", ["tenant_id"])
    op.create_index("ix_retrieval_records_span_id", "retrieval_records", ["span_id"])
    op.create_index(
        "ix_retrieval_records_tenant_created",
        "retrieval_records",
        ["tenant_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("retrieval_records")
    op.drop_index("ix_knowledge_chunks_content_trgm", table_name="knowledge_chunks")
    # 不 drop pg_trgm 扩展：其它对象可能仍依赖它。
