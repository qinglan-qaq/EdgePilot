"""Agent 业务逻辑：租户隔离的 CRUD、不可变版本发布与 Prompt 引用校验。

约定：
- 所有查询都带 ``tenant_id == get_tenant_id()``；不存在与跨租户统一 404。
- ``agent_versions`` 只有 INSERT 路径，任何代码都不得 UPDATE 该表。
- ``publish`` 在单事务内完成「锁 Agent 行 → 算下一版本 → 插入快照 →
  更新 current_version」，最后只提交一次。
- ``snapshot`` 只放 JSON 原生类型（int / str / bool / dict / list / null）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.core.tenancy import get_tenant_id
from app.models.agent import Agent, AgentToolBinding, AgentVersion
from app.models.run import AgentRun
from app.models.tool import Tool
from app.observability.models.prompt import PromptTemplate, PromptVersion

_UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "agent_type",
        "model_config_id",
        "prompt_template_id",
        "prompt_version",
        "config",
        "status",
    }
)

# 发布时由 current_version / 版本表推导，禁止通过 PATCH 直接写。
_FORBIDDEN_FIELDS = frozenset({"current_version", "id", "tenant_id", "created_at", "updated_at"})


def prompt_render_hash(version_row: PromptVersion) -> str:
    """Prompt 版本内容的稳定哈希；快照只存它，不存 Prompt 全文。"""
    canonical = json.dumps(
        {
            "system_prompt": version_row.system_prompt,
            "user_prompt_template": version_row.user_prompt_template,
            "variables_schema": version_row.variables_schema,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AgentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ CRUD
    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        agent_type: str,
        model_config_id: int | None = None,
        prompt_template_id: int | None = None,
        prompt_version: int | None = None,
        config: dict | None = None,
        status: str = "active",
        tool_ids: list[int] | None = None,
    ) -> Agent:
        await self._assert_tools_exist(tenant_id, tool_ids)
        row = Agent(
            tenant_id=tenant_id,
            name=name,
            agent_type=agent_type,
            model_config_id=model_config_id,
            prompt_template_id=prompt_template_id,
            prompt_version=prompt_version,
            config=config or {},
            status=status,
        )
        self.session.add(row)
        try:
            await self.session.flush()
            for tool_id in _dedupe(tool_ids):
                self.session.add(
                    AgentToolBinding(tenant_id=tenant_id, agent_id=row.id, tool_id=tool_id)
                )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("Agent 名称已存在") from None
        await self.session.refresh(row)
        return row

    async def update(self, agent_id: int, **fields: Any) -> Agent:
        row = await self.get(agent_id)
        tenant_id = get_tenant_id()

        tool_ids = fields.pop("tool_ids", None)
        for key in fields:
            if key in _FORBIDDEN_FIELDS or key not in _UPDATABLE_FIELDS:
                raise ValueError(f"不支持更新字段: {key}")

        try:
            for key, value in fields.items():
                setattr(row, key, value)
            if tool_ids is not None:
                await self._replace_tool_bindings(tenant_id, row.id, tool_ids)
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("Agent 名称已存在") from None
        await self.session.refresh(row)
        return row

    async def get(self, agent_id: int) -> Agent:
        row = await self.session.scalar(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == get_tenant_id(),
            )
        )
        if row is None:
            raise NotFound("Agent 不存在")
        return row

    async def list(self, page: int = 1, page_size: int = 20) -> tuple[list[Agent], int]:
        tenant_id = get_tenant_id()
        total = (
            await self.session.scalar(
                select(func.count()).select_from(Agent).where(Agent.tenant_id == tenant_id)
            )
            or 0
        )
        rows = (
            await self.session.scalars(
                select(Agent)
                .where(Agent.tenant_id == tenant_id)
                .order_by(Agent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return list(rows), total

    async def delete(self, agent_id: int) -> None:
        """删除从未运行过的 Agent；有运行历史一律 409，绝不销毁审计痕迹。"""
        row = await self.get(agent_id)
        tenant_id = get_tenant_id()
        has_runs = await self.session.scalar(
            select(AgentRun.id)
            .where(AgentRun.tenant_id == tenant_id, AgentRun.agent_id == agent_id)
            .limit(1)
        )
        if has_runs is not None:
            raise Conflict("Agent 存在运行记录，禁止删除")

        await self.session.execute(
            delete(AgentToolBinding).where(
                AgentToolBinding.tenant_id == tenant_id,
                AgentToolBinding.agent_id == agent_id,
            )
        )
        await self.session.execute(
            delete(AgentVersion).where(
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.agent_id == agent_id,
            )
        )
        await self.session.delete(row)
        await self.session.commit()

    # --------------------------------------------------------------- publish
    async def publish(self, agent_id: int, *, created_by: int | None = None) -> AgentVersion:
        """发布一个新版本；单事务内完成，快照不可变。"""
        tenant_id = get_tenant_id()
        agent = await self.session.scalar(
            select(Agent)
            .where(Agent.id == agent_id, Agent.tenant_id == tenant_id)
            .with_for_update()
        )
        if agent is None:
            raise NotFound("Agent 不存在")

        current_max = await self.session.scalar(
            select(func.coalesce(func.max(AgentVersion.version), 0)).where(
                AgentVersion.tenant_id == tenant_id,
                AgentVersion.agent_id == agent_id,
            )
        )
        version_no = int(current_max or 0) + 1

        snapshot = await self._build_snapshot(tenant_id, agent)
        row = AgentVersion(
            tenant_id=tenant_id,
            agent_id=agent_id,
            version=version_no,
            snapshot=snapshot,
            created_by=created_by,
        )
        self.session.add(row)
        agent.current_version = version_no
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def list_versions(self, agent_id: int) -> list[AgentVersion]:
        await self.get(agent_id)
        rows = (
            await self.session.scalars(
                select(AgentVersion)
                .where(
                    AgentVersion.tenant_id == get_tenant_id(),
                    AgentVersion.agent_id == agent_id,
                )
                .order_by(AgentVersion.version.desc())
            )
        ).all()
        return list(rows)

    async def list_runs(
        self, agent_id: int, page: int = 1, page_size: int = 20
    ) -> tuple[list[AgentRun], int]:
        await self.get(agent_id)
        tenant_id = get_tenant_id()
        total = (
            await self.session.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(AgentRun.tenant_id == tenant_id, AgentRun.agent_id == agent_id)
            )
            or 0
        )
        rows = (
            await self.session.scalars(
                select(AgentRun)
                .where(AgentRun.tenant_id == tenant_id, AgentRun.agent_id == agent_id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return list(rows), total

    # ---------------------------------------------------------------- helpers
    async def _build_snapshot(self, tenant_id: uuid.UUID, agent: Agent) -> dict:
        snapshot: dict = {
            "name": agent.name,
            "agent_type": agent.agent_type,
            "model_config_id": agent.model_config_id,
            "config": agent.config,
            "status": agent.status,
            "prompt": None,
        }
        if agent.prompt_template_id is not None:
            snapshot["prompt"] = await self._resolve_prompt_ref(tenant_id, agent)

        bindings = (
            await self.session.scalars(
                select(AgentToolBinding)
                .where(
                    AgentToolBinding.tenant_id == tenant_id,
                    AgentToolBinding.agent_id == agent.id,
                )
                .order_by(AgentToolBinding.id)
            )
        ).all()
        if bindings:
            snapshot["tools"] = [
                {"tool_id": binding.tool_id, "enabled": bool(binding.enabled)}
                for binding in bindings
            ]
        return snapshot

    async def _resolve_prompt_ref(self, tenant_id: uuid.UUID, agent: Agent) -> dict:
        template = await self.session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.tenant_id == tenant_id,
                PromptTemplate.id == agent.prompt_template_id,
            )
        )
        if template is None:
            raise Conflict("Prompt 模板不存在")

        version_no = agent.prompt_version
        if version_no is None:
            version_no = template.active_version
        if version_no is None:
            raise Conflict("Prompt 模板尚未发布")

        version_row = await self.session.scalar(
            select(PromptVersion).where(
                PromptVersion.tenant_id == tenant_id,
                PromptVersion.template_id == template.id,
                PromptVersion.version == version_no,
            )
        )
        if version_row is None:
            raise Conflict("Prompt 版本不存在")

        return {
            "template_id": template.id,
            "version": version_no,
            "rendered_hash": prompt_render_hash(version_row),
        }

    async def _assert_tools_exist(
        self, tenant_id: uuid.UUID, tool_ids: list[int] | None
    ) -> None:
        ids = _dedupe(tool_ids)
        if not ids:
            return
        found = set(
            (
                await self.session.scalars(
                    select(Tool.id).where(Tool.tenant_id == tenant_id, Tool.id.in_(ids))
                )
            ).all()
        )
        missing = sorted(set(ids) - found)
        if missing:
            raise NotFound(f"工具不存在: {missing}")

    async def _replace_tool_bindings(
        self, tenant_id: uuid.UUID, agent_id: int, tool_ids: list[int]
    ) -> None:
        ids = _dedupe(tool_ids)
        await self._assert_tools_exist(tenant_id, ids)
        await self.session.execute(
            delete(AgentToolBinding).where(
                AgentToolBinding.tenant_id == tenant_id,
                AgentToolBinding.agent_id == agent_id,
            )
        )
        for tool_id in ids:
            self.session.add(
                AgentToolBinding(tenant_id=tenant_id, agent_id=agent_id, tool_id=tool_id)
            )


def _dedupe(values: list[int] | None) -> list[int]:
    """去重且保留顺序；工具绑定有 (agent_id, tool_id) 唯一约束。"""
    return list(dict.fromkeys(values or []))
