from sqlalchemy import (
    BigInteger,
    Boolean,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class Agent(TenantMixin, TimestampMixin, Base):
    """Agent 定义；prompt 升为一等实体，这里只存引用与版本。"""

    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
        Index("ix_agents_tenant_type", "tenant_id", "agent_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_config_id: Mapped[int | None] = mapped_column(BigInteger)
    prompt_template_id: Mapped[int | None] = mapped_column(BigInteger)  # 不建外键
    prompt_version: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class AgentVersion(TenantMixin, TimestampMixin, Base):
    """发布后不可 UPDATE 的 Agent 快照。

    snapshot.prompt 约定为 ``{"template_id": ..., "version": ..., "rendered_hash": ...}``，
    只存引用 + 渲染哈希，不存 prompt 全文。
    """

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_versions_agent_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class AgentToolBinding(TenantMixin, TimestampMixin, Base):
    """Agent 与工具的绑定关系。"""

    __tablename__ = "agent_tool_bindings"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_id", name="uq_agent_tool_bindings_agent_tool"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    agent_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tool_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
