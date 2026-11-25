from sqlalchemy import (
    BigInteger,
    Boolean,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class Tool(TenantMixin, TimestampMixin, Base):
    """可被 Agent 绑定的工具（local handler 或 MCP 同步）。"""

    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("tenant_id", "tool_id", name="uq_tools_tenant_tool_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="local")
    input_schema: Mapped[dict | None] = mapped_column(JSONB)
    output_schema: Mapped[dict | None] = mapped_column(JSONB)
    permission_scope: Mapped[str | None] = mapped_column(String(64))
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="30000")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    side_effect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    handler: Mapped[str | None] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(32), nullable=False, server_default="1.0.0")
    dry_run_supported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
