"""Trace / Span 查询端点。租户过滤来自请求级 TenantContext。"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.pagination import PageParams
from app.db import get_db
from app.observability.services import trace_service
from app.schemas.common import ok

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/traces")
async def list_traces(
    params: PageParams = Depends(),
    status: str | None = None,
    trace_id: str | None = Query(default=None, description="trace_id 前缀"),
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    min_duration_ms: float | None = None,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    page = await trace_service.list_traces(
        session,
        params,
        status=status,
        trace_id_prefix=trace_id,
        start_from=start_from,
        start_to=start_to,
        min_duration_ms=min_duration_ms,
    )
    return ok(page.model_dump())


@router.get("/traces/{trace_id}")
async def get_trace(
    trace_id: str,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await trace_service.get_trace(session, trace_id))


@router.get("/traces/{trace_id}/tree")
async def get_trace_tree(
    trace_id: str,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await trace_service.get_tree(session, trace_id))


@router.get("/spans/{span_id}")
async def get_span(
    span_id: str,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await trace_service.get_span(session, span_id))


@router.get("/spans/{span_id}/children")
async def get_span_children(
    span_id: str,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await trace_service.get_span_children(session, span_id))
