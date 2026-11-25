from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    Identity,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class Span(Base, TenantMixin, TimestampMixin):
    """trace 内的单个执行节点；parent_span_id 为空即根 span。

    "metadata" 是 declarative 保留属性名，列名照旧，Python 侧用 meta 访问。
    """

    __tablename__ = "spans"
    __table_args__ = (
        Index("uq_spans_span_id", "span_id", unique=True),
        Index("ix_spans_trace_start", "trace_id", "start_time"),
        Index("ix_spans_tenant_start", "tenant_id", "start_time"),
        Index("ix_spans_tenant_parent", "tenant_id", "parent_span_id"),
        Index("ix_spans_tenant_type_start", "tenant_id", "span_type", "start_time"),
        Index("ix_spans_tenant_agent_start", "tenant_id", "agent_id", "start_time"),
        Index("ix_spans_tenant_tool_start", "tenant_id", "tool_id", "start_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    span_type: Mapped[str] = mapped_column(String(24), nullable=False)
    agent_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_version: Mapped[int | None] = mapped_column(Integer)
    workflow_id: Mapped[int | None] = mapped_column(BigInteger)
    node_id: Mapped[str | None] = mapped_column(String(64))
    tool_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Double)
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    meta: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    links: Mapped[list | dict | None] = mapped_column(JSONB)
