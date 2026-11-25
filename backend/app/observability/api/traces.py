"""Trace / Span 查询端点。租户过滤来自请求级 TenantContext。"""

from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.pagination import PageParams
from app.db import get_db, get_session_factory
from app.observability.live import get_pubsub
from app.observability.services import trace_service
from app.observability.sse import stream_trace_events
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


@router.get("/traces/{trace_id}/stream")
async def stream_trace(
    trace_id: str,
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    pubsub=Depends(get_pubsub),
    session_factory=Depends(get_session_factory),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """SSE：先补发 PG 历史事件，再转发 Redis 实时消息（事件 id = sequence_number）。"""
    await trace_service.get_trace(session, trace_id)
    return StreamingResponse(
        stream_trace_events(session_factory, pubsub, trace_id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
