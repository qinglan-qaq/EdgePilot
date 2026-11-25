"""工作流业务逻辑：草稿、校验、发布与版本读接口。

约定：

- 每个工作流恰好一条 ``version = 0`` 的草稿行；PATCH 只改这条草稿，
  ``version >= 1`` 的发布版本不可修改。
- 发布在单事务内完成「锁工作流行 → 校验草稿 → 取已发布最大版本 +1 →
  插入不可变版本 → 更新 current_version/status」，最后只提交一次。
- 审计是旁路：主事务 commit 后再写；审计失败不影响返回结果。
- service 不依赖 FastAPI，租户从上下文（``get_tenant_id``）读取。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.core.tenancy import get_tenant_id
from app.models.workflow import Workflow, WorkflowVersion
from app.repositories.workflow_repo import WorkflowRepo
from app.services.audit_service import AuditService
from app.workflow.dsl import dsl_hash
from app.workflow.validator import ValidationReport, assert_valid, validate_workflow

DEFAULT_DSL: dict[str, Any] = {"nodes": [], "edges": []}


@dataclass
class WorkflowDetail:
    """详情视图的原始数据；API 层再映射到 Pydantic 模型。"""

    workflow: Workflow
    dsl: dict
    draft_version: int | None
    last_run: dict | None = None


class WorkflowService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WorkflowRepo(session)

    # ------------------------------------------------------------------ CRUD
    async def list(
        self, page: int = 1, page_size: int = 20
    ) -> tuple[list[Workflow], dict[int, dict], int]:
        tenant_id = get_tenant_id()
        rows, total = await self.repo.list_workflows(tenant_id, page, page_size)
        summaries = await self.repo.last_runs_by_workflow(
            tenant_id, [row.id for row in rows]
        )
        return rows, summaries, total

    async def create(
        self,
        *,
        name: str,
        description: str | None = None,
        dsl: dict | None = None,
        created_by: int | None = None,
    ) -> WorkflowDetail:
        tenant_id = get_tenant_id()
        if await self.repo.get_by_name(tenant_id, name) is not None:
            raise Conflict("工作流名称已存在")

        dsl_value = copy.deepcopy(dsl) if dsl is not None else copy.deepcopy(DEFAULT_DSL)
        workflow = Workflow(
            tenant_id=tenant_id,
            name=name,
            description=description,
            status="draft",
            current_version=0,
            created_by=created_by,
        )
        self.session.add(workflow)
        try:
            await self.session.flush()
            self.session.add(
                WorkflowVersion(
                    tenant_id=tenant_id,
                    workflow_id=workflow.id,
                    version=0,
                    dsl=dsl_value,
                    dsl_hash=dsl_hash(dsl_value),
                    created_by=created_by,
                )
            )
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("工作流名称已存在") from None

        await self.session.refresh(workflow)
        return WorkflowDetail(
            workflow=workflow,
            dsl=copy.deepcopy(dsl_value),
            draft_version=0,
            last_run=None,
        )

    async def detail(self, workflow_id: int) -> WorkflowDetail:
        tenant_id = get_tenant_id()
        workflow = await self._get_or_404(tenant_id, workflow_id)

        draft = await self.repo.get_draft(tenant_id, workflow_id)
        if draft is not None:
            dsl = copy.deepcopy(draft.dsl)
            draft_version: int | None = draft.version
        else:
            published = await self._current_published(tenant_id, workflow)
            dsl = (
                copy.deepcopy(published.dsl)
                if published is not None
                else copy.deepcopy(DEFAULT_DSL)
            )
            draft_version = None

        last_run = (
            await self.repo.last_runs_by_workflow(tenant_id, [workflow.id])
        ).get(workflow.id)
        return WorkflowDetail(
            workflow=workflow,
            dsl=dsl,
            draft_version=draft_version,
            last_run=last_run,
        )

    async def update(self, workflow_id: int, fields: dict[str, Any]) -> WorkflowDetail:
        """只改元数据与草稿；已发布版本绝不触碰。"""
        tenant_id = get_tenant_id()
        workflow = await self._get_or_404(tenant_id, workflow_id)

        if "name" in fields and fields["name"] is not None:
            new_name = fields["name"]
            existing = await self.repo.get_by_name(tenant_id, new_name)
            if existing is not None and existing.id != workflow.id:
                raise Conflict("工作流名称已存在")
            workflow.name = new_name
        if "description" in fields:
            workflow.description = fields["description"]
        if "dsl" in fields and fields["dsl"] is not None:
            dsl_value = copy.deepcopy(fields["dsl"])
            draft = await self.repo.get_draft(tenant_id, workflow_id)
            if draft is None:
                draft = WorkflowVersion(
                    tenant_id=tenant_id,
                    workflow_id=workflow_id,
                    version=0,
                    dsl=dsl_value,
                    dsl_hash=dsl_hash(dsl_value),
                    created_by=workflow.created_by,
                )
                self.session.add(draft)
            else:
                draft.dsl = dsl_value
                draft.dsl_hash = dsl_hash(dsl_value)

        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("工作流名称已存在") from None
        await self.session.refresh(workflow)
        return await self.detail(workflow.id)

    # --------------------------------------------------------------- validate
    async def validate(
        self, workflow_id: int, dsl: dict | None = None
    ) -> ValidationReport:
        tenant_id = get_tenant_id()
        workflow = await self._get_or_404(tenant_id, workflow_id)

        if dsl is not None:
            target = dsl
        else:
            draft = await self.repo.get_draft(tenant_id, workflow_id)
            if draft is not None:
                target = draft.dsl
            else:
                published = await self._current_published(tenant_id, workflow)
                target = (
                    published.dsl
                    if published is not None
                    else copy.deepcopy(DEFAULT_DSL)
                )
        return await validate_workflow(self.session, tenant_id, target)

    # ---------------------------------------------------------------- publish
    async def publish(
        self,
        workflow_id: int,
        *,
        created_by: int | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> WorkflowVersion:
        tenant_id = get_tenant_id()
        workflow = await self.repo.get_workflow_for_update(tenant_id, workflow_id)
        if workflow is None:
            raise NotFound("工作流不存在")

        draft = await self.repo.get_draft(tenant_id, workflow_id)
        if draft is None:
            raise Conflict("工作流缺少草稿")

        report = await validate_workflow(self.session, tenant_id, draft.dsl)
        assert_valid(report)

        next_version = (
            await self.repo.max_published_version(tenant_id, workflow_id)
        ) + 1
        snapshot = copy.deepcopy(draft.dsl)
        row = WorkflowVersion(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            version=next_version,
            dsl=snapshot,
            dsl_hash=dsl_hash(snapshot),
            created_by=created_by,
        )
        self.session.add(row)
        workflow.current_version = next_version
        workflow.status = "published"
        await self.session.commit()
        await self.session.refresh(row)

        await AuditService.write(
            self.session,
            action="workflow.publish",
            resource_type="workflow",
            resource_id=workflow_id,
            detail={
                "workflow_id": workflow_id,
                "version": next_version,
                "dsl_hash": row.dsl_hash,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return row

    async def list_versions(self, workflow_id: int) -> list[WorkflowVersion]:
        tenant_id = get_tenant_id()
        await self._get_or_404(tenant_id, workflow_id)
        return await self.repo.list_published_versions(tenant_id, workflow_id)

    # ---------------------------------------------------------------- helpers
    async def _get_or_404(self, tenant_id: uuid.UUID, workflow_id: int) -> Workflow:
        workflow = await self.repo.get_workflow(tenant_id, workflow_id)
        if workflow is None:
            raise NotFound("工作流不存在")
        return workflow

    async def _current_published(
        self, tenant_id: uuid.UUID, workflow: Workflow
    ) -> WorkflowVersion | None:
        if workflow.current_version >= 1:
            return await self.repo.get_version(
                tenant_id, workflow.id, workflow.current_version
            )
        return None


__all__ = ["DEFAULT_DSL", "WorkflowDetail", "WorkflowService"]
