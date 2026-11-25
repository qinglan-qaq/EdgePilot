from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class EdgeNode(TenantMixin, TimestampMixin, Base):
    """边缘节点（常驻 edge-agent）。"""

    __tablename__ = "edge_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_key", name="uq_edge_nodes_tenant_node_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    node_key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    os: Mapped[str | None] = mapped_column(String(32))
    arch: Mapped[str | None] = mapped_column(String(32))
    agent_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="offline")
    ip: Mapped[str | None] = mapped_column(String(64))
    labels: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    metrics: Mapped[dict | None] = mapped_column(JSONB)  # cpu/mem/disk/gpu
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Device(TenantMixin, TimestampMixin, Base):
    """挂在边缘节点下的摄像头/传感器等设备。"""

    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    node_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="camera")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="offline")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
