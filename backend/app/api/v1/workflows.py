"""工作流路由：viewer 只读；operator/admin 可写；发布仅 admin。

校验刻意返回 200 + ``valid=false``（报告本身是结果，不是错误）；
只有发布路径在草稿非法时才抛 400（``detail.errors``）。
运行创建只落 pending 记录，不触发执行。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.api.v1.runs import run_detail_to_out
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.workflow import (
    RunSummaryOut,
    WorkflowCreate,
    WorkflowListItem,
    WorkflowOut,
    WorkflowRunCreate,
    WorkflowUpdate,
    WorkflowValidateIn,
    WorkflowVersionOut,
)
from app.services.run_service import RunService
from app.services.workflow_service import WorkflowDetail, WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])

WRITE_ROLES = ("admin", "operator")


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


def workflow_to_out(detail: WorkflowDetail) -> WorkflowOut:
    workflow = detail.workflow
    return WorkflowOut(
        id=workflow.id,
        name=workflow.name,
        description=workflow.description,
        status=workflow.status,
        current_version=workflow.current_version,
        draft_version=detail.draft_version,
        dsl=detail.dsl,
        created_by=workflow.created_by,
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
        last_run=(
            RunSummaryOut(**detail.last_run) if detail.last_run is not None else None
        ),
    )


@router.get("")
async def list_workflows(
    params: PageParams = Depends(),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, summaries, total = await WorkflowService(session).list(
        params.page, params.size
    )
    items = [
        WorkflowListItem(
            id=row.id,
            name=row.name,
            description=row.description,
            status=row.status,
            current_version=row.current_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
            last_run=(
                RunSummaryOut(**summaries[row.id])
                if row.id in summaries
                else None
            ),
        )
        for row in rows
    ]
    page = PageResult[WorkflowListItem](
        items=items, total=total, page=params.page, size=params.size
    )
    return ok(page.model_dump())


@router.post("")
async def create_workflow(
    payload: WorkflowCreate,
    user: User = Depends(require_role(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
) -> dict:
    detail = await WorkflowService(session).create(
        name=payload.name,
        description=payload.description,
        dsl=payload.dsl,
        created_by=user.id,
    )
    return ok(workflow_to_out(detail).model_dump())


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    detail = await WorkflowService(session).detail(workflow_id)
    return ok(workflow_to_out(detail).model_dump())


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: int,
    payload: WorkflowUpdate,
    _: User = Depends(require_role(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
) -> dict:
    detail = await WorkflowService(session).update(
        workflow_id, payload.model_dump(exclude_unset=True)
    )
    return ok(workflow_to_out(detail).model_dump())


@router.post("/{workflow_id}/validate")
async def validate_workflow_dsl(
    workflow_id: int,
    payload: WorkflowValidateIn | None = None,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """只读校验；非法也返回 200，由 ``valid`` / ``errors`` 表达。"""
    dsl = payload.dsl if payload is not None else None
    report = await WorkflowService(session).validate(workflow_id, dsl=dsl)
    return ok(report.to_dict())


@router.post("/{workflow_id}/publish")
async def publish_workflow(
    workflow_id: int,
    request: Request,
    user: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    ip, user_agent = _client_meta(request)
    row = await WorkflowService(session).publish(
        workflow_id,
        created_by=user.id,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    return ok(WorkflowVersionOut.model_validate(row).model_dump())


@router.get("/{workflow_id}/versions")
async def list_workflow_versions(
    workflow_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows = await WorkflowService(session).list_versions(workflow_id)
    return ok([WorkflowVersionOut.model_validate(row).model_dump() for row in rows])


@router.post("/{workflow_id}/runs")
async def create_workflow_run(
    workflow_id: int,
    request: Request,
    payload: WorkflowRunCreate | None = None,
    user: User = Depends(require_role(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """创建 pending 运行记录；不触发执行、不生成 trace_id。"""
    body = payload or WorkflowRunCreate()
    ip, user_agent = _client_meta(request)
    ctx = await RunService(session).create(
        workflow_id,
        input_data=body.input,
        trigger_type=body.trigger_type,
        trigger_payload=body.trigger_payload,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    return ok(run_detail_to_out(ctx).model_dump())
