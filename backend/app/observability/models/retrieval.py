"""检索记录 ORM（0004_obs_retrieval）。

与 ``app/observability/writer.py`` 的 ``_RETRIEVAL_COLUMNS`` 完全对齐：

- id / tenant_id / span_id / query 为核心定位字段；
- 候选文档与最终文档都是 JSONB 数组，分数由检索器真实计算；
- 只有 ``created_at``，没有 ``updated_at``；
- 不建跨表外键（保留期独立清理）。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin


class RetrievalRecord(TenantMixin, Base):
    __tablename__ = "retrieval_records"
    __table_args__ = (
        Index("ix_retrieval_records_span_id", "span_id"),
        Index(
            "ix_retrieval_records_tenant_created",
            "tenant_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    retrieval_strategy: Mapped[str | None] = mapped_column(String(32))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    vector_store: Mapped[str | None] = mapped_column(String(64))
    reranker_model: Mapped[str | None] = mapped_column(String(128))
    top_k: Mapped[int | None] = mapped_column(Integer)
    similarity_threshold: Mapped[float | None] = mapped_column(Float)
    retrieved_documents: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="'[]'::jsonb"
    )
    reranked_documents: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="'[]'::jsonb"
    )
    final_context: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    error: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
