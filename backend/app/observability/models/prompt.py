"""Prompt 模板 / 不可变版本 / 执行记录。

字段与唯一约束以 migrations/versions/0003_obs_llm_tool_prompt.py 为准，
`alembic check` 无差异。prompt_versions 只有 INSERT 路径，发布与回滚只改
prompt_templates.active_version。
"""

from sqlalchemy import (
    BigInteger,
    Identity,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class PromptTemplate(Base, TenantMixin, TimestampMixin):
    """模板主表；template_id 在租户内唯一，active_version 指向当前发布版本。"""

    __tablename__ = "prompt_templates"
    __table_args__ = (
        Index("ix_prompt_templates_created_at", "created_at"),
        Index(
            "uq_prompt_templates_tenant_template",
            "tenant_id",
            "template_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    template_name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(32))
    owner: Mapped[str | None] = mapped_column(String(64))
    active_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")


class PromptVersion(Base, TenantMixin, TimestampMixin):
    """版本行只追加、不可变更；版本号在模板行锁内自增。"""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index("ix_prompt_versions_created_at", "created_at"),
        Index(
            "uq_prompt_versions_template_version",
            "template_id",
            "version",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    template_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    user_prompt_template: Mapped[str | None] = mapped_column(Text)
    variables_schema: Mapped[dict | None] = mapped_column(JSONB)
    model_config: Mapped[dict | None] = mapped_column(JSONB)
    changelog: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class PromptExecution(Base, TenantMixin, TimestampMixin):
    """一次渲染/执行记录；由 Task 8 的 recorder 写入。"""

    __tablename__ = "prompt_executions"
    __table_args__ = (
        Index("ix_prompt_executions_created_at", "created_at"),
        Index("ix_prompt_executions_trace_id", "trace_id"),
        Index("ix_prompt_executions_span_id", "span_id"),
        Index(
            "ix_prompt_executions_template_version_created",
            "template_id",
            "template_version",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    template_id: Mapped[int | None] = mapped_column(BigInteger)
    template_version: Mapped[int | None] = mapped_column(Integer)
    variables: Mapped[dict | None] = mapped_column(JSONB)
    rendered_messages: Mapped[list | dict | None] = mapped_column(JSONB)
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    render_status: Mapped[str] = mapped_column(String(24), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(128))
