"""Prompt 模板版本管理。

- 版本行不可变：本模块对 prompt_versions 只有 INSERT，没有 UPDATE 路径。
- 发布 / 回滚只修改 prompt_templates.active_version。
- 所有查询都带 tenant_id == get_tenant_id()，跨租户不可见。
"""

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.core.pagination import PageParams, PageResult
from app.core.tenancy import get_tenant_id
from app.observability.models import PromptExecution, PromptTemplate, PromptVersion


def template_summary(row: PromptTemplate) -> dict:
    return {
        "id": row.id,
        "template_id": row.template_id,
        "template_name": row.template_name,
        "category": row.category,
        "active_version": row.active_version,
        "status": row.status,
        "updated_at": row.updated_at,
        "created_at": row.created_at,
    }


def template_detail(row: PromptTemplate) -> dict:
    return {
        "id": row.id,
        "template_id": row.template_id,
        "template_name": row.template_name,
        "description": row.description,
        "category": row.category,
        "owner": row.owner,
        "active_version": row.active_version,
        "status": row.status,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def execution_dict(row: PromptExecution) -> dict:
    """对外暴露的执行记录字段；缺失值保持 null，不做补齐。"""
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "template_version": row.template_version,
        "render_status": row.render_status,
        "model_name": row.model_name,
        "prompt_hash": row.prompt_hash,
        "variables": row.variables,
        "rendered_messages": row.rendered_messages,
        "created_at": row.created_at,
    }


def version_dict(row: PromptVersion) -> dict:
    return {
        "id": row.id,
        "template_id": row.template_id,
        "version": row.version,
        "system_prompt": row.system_prompt,
        "user_prompt_template": row.user_prompt_template,
        "variables_schema": row.variables_schema,
        "model_config": row.model_config,
        "changelog": row.changelog,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


async def create_template(
    session: AsyncSession,
    *,
    template_id: str,
    template_name: str,
    description: str | None = None,
    category: str | None = None,
    owner: str | None = None,
) -> PromptTemplate:
    tenant_id = get_tenant_id()
    exists = await session.scalar(
        select(PromptTemplate.id).where(
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.template_id == template_id,
        )
    )
    if exists is not None:
        raise Conflict("模板已存在")

    row = PromptTemplate(
        tenant_id=tenant_id,
        template_id=template_id,
        template_name=template_name,
        description=description,
        category=category,
        owner=owner,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise Conflict("模板已存在") from None
    return row


async def add_version(
    session: AsyncSession,
    template_pk: int,
    *,
    system_prompt: str | None = None,
    user_prompt_template: str | None = None,
    variables_schema: dict | None = None,
    model_config: dict | None = None,
    changelog: str | None = None,
    created_by: int | None = None,
) -> PromptVersion:
    """追加版本。在模板行锁内取 max(version)+1，旧版本行绝不 UPDATE。"""
    tenant_id = get_tenant_id()
    template = await session.scalar(
        select(PromptTemplate)
        .where(
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.id == template_pk,
        )
        .with_for_update()
    )
    if template is None:
        raise NotFound("模板不存在")

    current = await session.scalar(
        select(func.coalesce(func.max(PromptVersion.version), 0)).where(
            PromptVersion.tenant_id == tenant_id,
            PromptVersion.template_id == template_pk,
        )
    )
    row = PromptVersion(
        tenant_id=tenant_id,
        template_id=template_pk,
        version=int(current or 0) + 1,
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        variables_schema=variables_schema,
        model_config=model_config,
        changelog=changelog,
        created_by=created_by,
    )
    session.add(row)
    await session.commit()
    return row


async def _assert_version_exists(
    session: AsyncSession, tenant_id, template_pk: int, version: int
) -> None:
    found = await session.scalar(
        select(PromptVersion.id).where(
            PromptVersion.tenant_id == tenant_id,
            PromptVersion.template_id == template_pk,
            PromptVersion.version == version,
        )
    )
    if found is None:
        raise NotFound("版本不存在")


async def publish(
    session: AsyncSession, template_pk: int, version: int
) -> PromptTemplate:
    """发布 = 把 active_version 指向已存在的版本，不改任何版本内容。"""
    tenant_id = get_tenant_id()
    template = await get_template(session, template_pk)
    await _assert_version_exists(session, tenant_id, template_pk, version)

    await session.execute(
        update(PromptTemplate)
        .where(
            PromptTemplate.tenant_id == tenant_id,
            PromptTemplate.id == template_pk,
        )
        .values(active_version=version)
    )
    await session.commit()
    await session.refresh(template)
    return template


async def rollback(
    session: AsyncSession, template_pk: int, version: int
) -> PromptTemplate:
    """回滚语义与发布相同：只改 active_version。"""
    return await publish(session, template_pk, version)


async def get_template(session: AsyncSession, template_pk: int) -> PromptTemplate:
    row = await session.scalar(
        select(PromptTemplate).where(
            PromptTemplate.tenant_id == get_tenant_id(),
            PromptTemplate.id == template_pk,
        )
    )
    if row is None:
        raise NotFound("模板不存在")
    return row


async def list_templates(
    session: AsyncSession, params: PageParams
) -> PageResult[dict]:
    tenant_id = get_tenant_id()
    total = await session.scalar(
        select(func.count())
        .select_from(PromptTemplate)
        .where(PromptTemplate.tenant_id == tenant_id)
    )
    rows = (
        await session.scalars(
            select(PromptTemplate)
            .where(PromptTemplate.tenant_id == tenant_id)
            .order_by(PromptTemplate.updated_at.desc(), PromptTemplate.id.desc())
            .offset(params.offset)
            .limit(params.size)
        )
    ).all()
    return PageResult(
        items=[template_summary(row) for row in rows],
        total=total or 0,
        page=params.page,
        size=params.size,
    )


async def list_versions(
    session: AsyncSession, template_pk: int
) -> list[PromptVersion]:
    await get_template(session, template_pk)
    return list(
        (
            await session.scalars(
                select(PromptVersion)
                .where(
                    PromptVersion.tenant_id == get_tenant_id(),
                    PromptVersion.template_id == template_pk,
                )
                .order_by(PromptVersion.version.desc())
            )
        ).all()
    )


async def list_executions(
    session: AsyncSession, template_pk: int, params: PageParams
) -> PageResult[dict]:
    """分页读取模板的执行历史，按 created_at、id 倒序。

    先校验模板属于当前租户：跨租户与不存在一律 404。统计与分页查询都同时
    限定 ``tenant_id`` 与数字模板主键，绝不返回其他租户数据。
    """
    await get_template(session, template_pk)
    tenant_id = get_tenant_id()
    filters = (
        PromptExecution.tenant_id == tenant_id,
        PromptExecution.template_id == template_pk,
    )
    total = await session.scalar(
        select(func.count()).select_from(PromptExecution).where(*filters)
    )
    rows = (
        await session.scalars(
            select(PromptExecution)
            .where(*filters)
            .order_by(PromptExecution.created_at.desc(), PromptExecution.id.desc())
            .offset(params.offset)
            .limit(params.size)
        )
    ).all()
    return PageResult(
        items=[execution_dict(row) for row in rows],
        total=total or 0,
        page=params.page,
        size=params.size,
    )


async def get_active(
    session: AsyncSession, template_pk: int
) -> tuple[int, PromptVersion]:
    template = await get_template(session, template_pk)
    if template.active_version is None:
        raise NotFound("尚未发布")
    version = await session.scalar(
        select(PromptVersion).where(
            PromptVersion.tenant_id == get_tenant_id(),
            PromptVersion.template_id == template_pk,
            PromptVersion.version == template.active_version,
        )
    )
    if version is None:
        raise NotFound("版本不存在")
    return template.active_version, version
