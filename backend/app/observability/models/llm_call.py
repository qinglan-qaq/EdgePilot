from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Float,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class LLMCall(Base, TenantMixin, TimestampMixin):
    """一次模型调用；归属于某个 span，按 attempt 1:N 追加。"""

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_created_at", "created_at"),
        Index("ix_llm_calls_span_attempt", "span_id", "attempt"),
        Index("ix_llm_calls_tenant_model_created", "tenant_id", "model_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    temperature: Mapped[float | None] = mapped_column(Float)
    top_p: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    tool_definitions: Mapped[list | dict | None] = mapped_column(JSONB)
    tool_choice: Mapped[str | None] = mapped_column(String(32))
    request_messages: Mapped[list | dict | None] = mapped_column(JSONB)
    raw_response: Mapped[dict | None] = mapped_column(JSONB)
    structured_response: Mapped[dict | None] = mapped_column(JSONB)
    response_format: Mapped[str | None] = mapped_column(String(32))
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    request_id: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[dict | None] = mapped_column(JSONB)
