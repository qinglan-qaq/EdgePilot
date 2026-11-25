"""运行记录路由：登录即可读；operator/admin 可取消。

运行详情与节点时间线只读数据库现状，不触发任何执行逻辑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.workflow import (
    RunLogOut,
    WorkflowNodeRunOut,
    WorkflowRunDetailOut,
    WorkflowRunOut,
)
from app.services.run_service import RunContext, RunService

router = APIRouter(prefix="/runs", tags=["runs"])


def run_to_out(ctx: RunContext) -> WorkflowRunOut:
    run = ctx.run
    return WorkflowRunOut(
        id=run.id,
        workflow_id=ctx.workflow_id,
        workflow_name=ctx.workflow_name,
        workflow_version_id=run.workflow_version_id,
        status=run.status,
        trigger_type=run.trigger_type,
        trigger_payload=run.trigger_payload,
        input=run.input,
        output=run.output,
        error=run.error,
        trace_id=run.trace_id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def run_detail_to_out(ctx: RunContext) -> WorkflowRunDetailOut:
    return WorkflowRunDetailOut(
        **run_to_out(ctx).model_dump(),
        nodes=[WorkflowNodeRunOut.model_validate(node) for node in ctx.nodes],
    )


@router.get("")
async def list_runs(
    params: PageParams = Depends(),
    workflow_id: int | None = None,
    status: str | None = None,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await RunService(session).list(
        params.page,
        params.size,
        workflow_id=workflow_id,
        status=status,
    )
    items = [
        run_to_out(
            RunContext(
                run=row.run,
                workflow_id=row.workflow_id,
                workflow_name=row.workflow_name,
                dsl={},
                nodes=[],
            )
        )
        for row in rows
    ]
    page = PageResult[WorkflowRunOut](
        items=items, total=total, page=params.page, size=params.size
    )
    return ok(page.model_dump())


@router.get("/{run_id}")
async def get_run(
    run_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    ctx = await RunService(session).detail(run_id)
    return ok(run_detail_to_out(ctx).model_dump())


@router.get("/{run_id}/logs")
async def get_run_logs(
    run_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    service = RunService(session)
    ctx = await service.detail(run_id)
    logs = [
        RunLogOut(
            node_id=node.node_id,
            node_type=node.node_type,
            status=node.status,
            attempt=node.attempt,
            started_at=node.started_at,
            finished_at=node.finished_at,
            error=node.error,
        ).model_dump()
        for node in service.logs(ctx)
    ]
    return ok(logs)


@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    request: Request,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    ip = request.client.host if request.client else None
    ctx = await RunService(session).cancel(
        run_id,
        user=user,
        ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(run_detail_to_out(ctx).model_dump())
