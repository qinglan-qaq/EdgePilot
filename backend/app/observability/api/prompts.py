"""Prompt 模板 API：创建、列表、详情、追加版本、发布、回滚与 Playground 预览/执行。

写操作仅 admin；版本行没有 PATCH/PUT 端点（不可变）。Playground 的
``execute=True`` 会真实调用 LLMClient 并写入 prompt_executions。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import LLMClient
from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.core.exceptions import Forbidden, NotFound
from app.core.pagination import PageParams
from app.core.tenancy import get_tenant_id
from app.db import get_db
from app.models.model_config import ModelConfig
from app.observability import get_tracer
from app.observability.models import PromptVersion
from app.observability.prompt import version_manager
from app.observability.prompt.recorder import record_execution
from app.observability.prompt.renderer import RenderResult, render
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
    # 可选：指定本次执行使用的模型配置；不传则用 LLMClient 默认行为
    model_config_id: int | None = None


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


async def _resolve_playground_model(
    session: AsyncSession, model_config_id: int | None
) -> ModelConfig | None:
    """按可选 ``model_config_id`` 取本租户模型配置；跨租户/不存在一律 404。"""
    if model_config_id is None:
        return None
    row = await session.scalar(
        select(ModelConfig).where(
            ModelConfig.id == model_config_id,
            ModelConfig.tenant_id == get_tenant_id(),
        )
    )
    if row is None:
        raise NotFound("模型不存在")
    return row


def _preview_payload(version_row: PromptVersion, result: RenderResult) -> dict:
    """渲染结果的通用响应字段；预览与执行成功分支共用。"""
    return {
        "status": result.status,
        "messages": result.messages,
        "prompt_hash": result.prompt_hash,
        "version": version_row.version,
    }


@router.post("/{template_pk}/playground")
async def playground(
    template_pk: int,
    payload: PlaygroundRequest,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """渲染预览；``execute=True`` 时真实调用 LLM 并记录本次 Prompt 执行。

    - 预览本身没有 trace/span，因此不写 prompt_executions。
    - ``execute=True`` 需要 admin/operator；渲染失败（schema/render）属于用户
      错误，直接返回预览 + ``response=None``，不调用 LLM、不写执行记录。
    - 真实执行开一条 ``source="playground"`` 的 trace，当前 span 记录 prompt
      元信息；LLMClient 自己把 LLMCallRecord 挂到该 span（不写 agent_runs）。
    - LLM 超时/错误仍返回 200 + ``status="llm_error"``，只有鉴权/404/422 非 200。
    """
    version_row = await _resolve_playground_version(session, template_pk, payload.version)

    if not payload.execute:
        result = render(version_row, payload.variables)
        return ok(_preview_payload(version_row, result))

    if user.role not in ("admin", "operator"):
        raise Forbidden("角色不足")

    result = render(version_row, payload.variables)
    if result.status != "ok":
        # 渲染失败不触达 LLM，也不产生执行记录；保持与预览一致的响应形状。
        return ok(
            {
                **_preview_payload(version_row, result),
                "trace_id": None,
                "span_id": None,
                "response": None,
            }
        )

    model_row = await _resolve_playground_model(session, payload.model_config_id)
    settings = get_settings()
    tracer = get_tracer()
    llm = LLMClient(mode=settings.llm_mode, fixture_dir=settings.llm_fixture_dir)

    async with tracer.start_trace(
        "playground",
        source="playground",
        input={
            "template_id": version_row.template_id,
            "version": version_row.version,
            "variables": payload.variables,
        },
    ) as trace:
        async with tracer.start_span(
            f"prompt.{version_row.template_id}",
            "prompt",
            version=version_row.version,
        ) as span:
            span.set_meta(
                template_id=version_row.template_id,
                template_version=version_row.version,
                prompt_hash=result.prompt_hash,
            )
            resp = await llm.chat(result.messages, model_config=model_row)

    exec_status = "ok" if not resp.error_code else "llm_error"
    # 记录失败不影响响应：recorder 自己吞掉 DB 异常并返回 None。
    await record_execution(
        session,
        trace_id=trace.trace_id or None,
        span_id=span.span_id or None,
        template_id=version_row.template_id,
        version=version_row.version,
        variables=payload.variables,
        messages=result.messages,
        status=exec_status,
        prompt_hash=result.prompt_hash,
        model_name=resp.model_name,
    )
    return ok(
        {
            **_preview_payload(version_row, result),
            "status": exec_status,
            "trace_id": trace.trace_id or None,
            "span_id": span.span_id or None,
            "response": {
                "content": resp.content,
                "structured": resp.structured,
                "usage": resp.usage,
                "finish_reason": resp.finish_reason,
                "provider": resp.provider,
                "model_name": resp.model_name,
                "request_id": resp.request_id,
                "cost": str(resp.cost) if resp.cost is not None else None,
                "error_code": resp.error_code,
                "degraded": resp.degraded,
            },
        }
    )
