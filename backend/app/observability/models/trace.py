from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Double,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class Trace(Base, TenantMixin, TimestampMixin):
    """一次端到端调用的根记录。

    "metadata" 是 declarative 保留属性名，列名照旧，Python 侧用 meta 访问。
    """

    __tablename__ = "traces"
    __table_args__ = (
        Index("uq_traces_trace_id", "trace_id", unique=True),
        Index("ix_traces_tenant_start", "tenant_id", "start_time"),
        Index("ix_traces_tenant_status_start", "tenant_id", "status", "start_time"),
        Index("ix_traces_metadata_gin", "metadata", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(64))
    root_span_id: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="running")
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Double)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    cost_unavailable_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    total_spans: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    meta: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
