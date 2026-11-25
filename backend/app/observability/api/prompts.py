"""Prompt 模板 API：创建、列表、详情、追加版本、发布、回滚与 Playground 预览。

写操作仅 admin；版本行没有 PATCH/PUT 端点（不可变）。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.exceptions import Forbidden, NotFound, ServiceUnavailable
from app.core.pagination import PageParams
from app.core.tenancy import get_tenant_id
from app.db import get_db
from app.observability.models import PromptVersion
from app.observability.prompt import version_manager
from app.observability.prompt.renderer import render
from app.schemas.common import ok

router = APIRouter(prefix="/observability/prompts", tags=["prompts"])


class CreateTemplateRequest(BaseModel):
    template_id: str
    template_name: str
    description: str | None = None
    category: str | None = None
    owner: str | None = None


class AddVersionRequest(BaseModel):
    # model_config 是 pydantic 保留名，字段用别名对外暴露为 model_config
    model_config = ConfigDict(populate_by_name=True)

    system_prompt: str | None = None
    user_prompt_template: str | None = None
    variables_schema: dict | None = None
    model_config_json: dict | None = Field(default=None, alias="model_config")
    changelog: str | None = None


class VersionRequest(BaseModel):
    version: int


class PlaygroundRequest(BaseModel):
    version: int | None = None
    variables: dict = Field(default_factory=dict)
    execute: bool = False


@router.post("")
async def create_template(
    payload: CreateTemplateRequest,
    _user=Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await version_manager.create_template(
        session,
        template_id=payload.template_id,
        template_name=payload.template_name,
        description=payload.description,
        category=payload.category,
        owner=payload.owner,
    )
    return ok(
        {
            "id": row.id,
            "template_id": row.template_id,
            "template_name": row.template_name,
            "active_version": row.active_version,
            "status": row.status,
        }
    )


@router.get("")
async def list_templates(
    params: PageParams = Depends(),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    page = await version_manager.list_templates(session, params)
    return ok(page.model_dump())


@router.get("/{template_pk}")
async def get_template(
    template_pk: int,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    template = await version_manager.get_template(session, template_pk)
    versions = await version_manager.list_versions(session, template_pk)
    detail = version_manager.template_detail(template)
    detail["versions"] = [version_manager.version_dict(row) for row in versions]
    return ok(detail)


@router.post("/{template_pk}/versions")
async def add_version(
    template_pk: int,
    payload: AddVersionRequest,
    user=Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await version_manager.add_version(
        session,
        template_pk,
        system_prompt=payload.system_prompt,
        user_prompt_template=payload.user_prompt_template,
        variables_schema=payload.variables_schema,
        model_config=payload.model_config_json,
        changelog=payload.changelog,
        created_by=user.id,
    )
    return ok(version_manager.version_dict(row))


@router.post("/{template_pk}/publish")
async def publish(
    template_pk: int,
    payload: VersionRequest,
    _user=Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    template = await version_manager.publish(session, template_pk, payload.version)
    return ok({"active_version": template.active_version})


@router.post("/{template_pk}/rollback")
async def rollback(
    template_pk: int,
    payload: VersionRequest,
    _user=Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    template = await version_manager.rollback(session, template_pk, payload.version)
    return ok({"active_version": template.active_version})


async def _resolve_playground_version(
    session: AsyncSession, template_pk: int, requested: int | None
) -> PromptVersion:
    """指定版本需属于该模板；未指定则用已发布版本。"""
    template = await version_manager.get_template(session, template_pk)
    version_no = requested if requested is not None else template.active_version
    if version_no is None:
        raise NotFound("尚未发布")
    row = await session.scalar(
        select(PromptVersion).where(
            PromptVersion.tenant_id == get_tenant_id(),
            PromptVersion.template_id == template_pk,
            PromptVersion.version == version_no,
        )
    )
    if row is None:
        raise NotFound("版本不存在")
    return row


@router.post("/{template_pk}/playground")
async def playground(
    template_pk: int,
    payload: PlaygroundRequest,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """渲染预览；预览本身没有 trace/span，因此不写 prompt_executions。

    execute=True 需要 admin/operator。真实执行要等平台 P2 的
    `app.llm.client.LLMClient` 落地后替换为：开新 trace(source="playground")
    调 `LLMClient.chat`，再用 record_execution 记录本次 Prompt 执行。
    """
    version_row = await _resolve_playground_version(session, template_pk, payload.version)

    if payload.execute:
        if user.role not in ("admin", "operator"):
            raise Forbidden("角色不足")
        raise ServiceUnavailable("LLM 客户端尚未接入，Playground 暂支持渲染预览")

    result = render(version_row, payload.variables)
    return ok(
        {
            "status": result.status,
            "messages": result.messages,
            "prompt_hash": result.prompt_hash,
            "version": version_row.version,
        }
    )
