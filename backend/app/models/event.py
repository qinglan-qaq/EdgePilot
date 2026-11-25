from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class VisionEvent(TenantMixin, TimestampMixin, Base):
    """边缘上报的视觉事件；dedup_key 幂等去重。"""

    __tablename__ = "vision_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedup_key", name="uq_vision_events_tenant_dedup"),
        Index("ix_vision_events_tenant_occurred", "tenant_id", text("occurred_at DESC")),
        Index("ix_vision_events_device_occurred", "device_id", text("occurred_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    node_id: Mapped[int | None] = mapped_column(BigInteger)
    device_id: Mapped[int | None] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)  # zone|line|dwell|count|anomaly
    severity: Mapped[str | None] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    track_id: Mapped[str | None] = mapped_column(String(64))
    snapshot_path: Mapped[str | None] = mapped_column(String(512))
    bbox: Mapped[dict | None] = mapped_column(JSONB)
    model_version: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="new")
