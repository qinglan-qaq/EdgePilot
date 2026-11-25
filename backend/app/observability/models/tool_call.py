from typing import Any

from sqlalchemy import (
    BigInteger,
    Float,
    Identity,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class ToolCall(Base, TenantMixin, TimestampMixin):
    """一次工具调用；归属于某个 span，重试时每次 attempt 一行。"""

    __tablename__ = "tool_calls"
    __table_args__ = (
        Index("ix_tool_calls_created_at", "created_at"),
        Index("ix_tool_calls_span_attempt", "span_id", "attempt"),
        Index("ix_tool_calls_tenant_tool_created", "tenant_id", "tool_id", "created_at"),
        Index(
            "ix_tool_calls_tenant_status_created",
            "tenant_id",
            "execution_status",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    input_schema: Mapped[dict | None] = mapped_column(JSONB)
    output_schema: Mapped[dict | None] = mapped_column(JSONB)
    input_arguments: Mapped[dict | None] = mapped_column(JSONB)
    output_result: Mapped[Any] = mapped_column(JSONB, nullable=True)
    execution_status: Mapped[str] = mapped_column(String(24), nullable=False)
    permission_result: Mapped[str | None] = mapped_column(String(24))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[dict | None] = mapped_column(JSONB)
