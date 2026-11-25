"""Prompt 模板 API：创建、列表、详情、追加版本、发布与回滚。

写操作仅 admin；版本行没有 PATCH/PUT 端点（不可变）。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.pagination import PageParams
from app.db import get_db
from app.observability.prompt import version_manager
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
