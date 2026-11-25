from decimal import Decimal

from sqlalchemy import BigInteger, Identity, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class ModelConfig(TenantMixin, TimestampMixin, Base):
    """LLM / embedding / reranker 模型配置；api_key_enc 由 Fernet 加密。"""

    __tablename__ = "model_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_model_configs_tenant_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)  # llm|embedding|reranker
    provider: Mapped[str] = mapped_column(String(32), nullable=False)  # deepseek|openai|local|mock
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512))
    api_key_enc: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    deployment_location: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    # 三档单价，任一必要单价为空 ⇒ cost = null，不估算
    input_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    output_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    cached_price_per_1k: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
