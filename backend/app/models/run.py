from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class WorkflowRun(TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index(
            "ix_workflow_runs_tenant_status_created",
            "tenant_id",
            "status",
            text("created_at DESC"),
        ),
        Index("ix_workflow_runs_trace_id", "trace_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workflow_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="manual")
    trigger_payload: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    root_span_id: Mapped[str | None] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkflowNodeRun(TenantMixin, TimestampMixin, Base):
    __tablename__ = "workflow_node_runs"
    __table_args__ = (
        UniqueConstraint("run_id", "node_id", name="uq_workflow_node_runs_run_node"),
        Index(
            "ix_workflow_node_runs_waiting_approval",
            "status",
            postgresql_where=text("status = 'waiting_approval'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    approval: Mapped[dict | None] = mapped_column(JSONB)  # decision/by/at/comment/expires_at
    error: Mapped[str | None] = mapped_column(Text)
    span_id: Mapped[str | None] = mapped_column(String(16))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(TenantMixin, TimestampMixin, Base):
    """Agent 单次运行；token/cost 由 agent span 关闭时聚合回写一次，供列表页免 join。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_agent_created", "agent_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workflow_run_id: Mapped[int | None] = mapped_column(BigInteger)
    node_run_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    agent_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    input: Mapped[dict | None] = mapped_column(JSONB)
    output: Mapped[dict | None] = mapped_column(JSONB)
    span_id: Mapped[str | None] = mapped_column(String(16))
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
