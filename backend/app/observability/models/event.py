from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin


class TraceEvent(Base, TenantMixin):
    """trace 内的时序事件；只追加，无 updated_at。"""

    __tablename__ = "trace_events"
    __table_args__ = (
        Index("uq_events_trace_seq", "trace_id", "sequence_number", unique=True),
        Index("ix_events_trace_created", "trace_id", "created_at"),
    )


    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    span_id: Mapped[str | None] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_data: Mapped[dict | None] = mapped_column(JSONB)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
