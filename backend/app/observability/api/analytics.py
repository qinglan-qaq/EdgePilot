"""观测分析端点：全部 GET、viewer 可读、租户来自请求级 TenantContext。

路径前缀 ``/api/v1/observability/analytics``。查询参数 ``start_from`` /
``start_to`` 为可选 ISO datetime；缺省窗口为最近 24 小时（UTC），并由
overview 的 ``window`` 字段回显解析结果。非法 datetime 由 FastAPI 参数
校验返回 422。
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_db
from app.observability.services import analytics_service
from app.schemas.common import ok

router = APIRouter(prefix="/observability/analytics", tags=["analytics"])


def resolved_window(
    start_from: datetime | None = Query(
        default=None, description="窗口起点（含），ISO datetime"
    ),
    start_to: datetime | None = Query(
        default=None, description="窗口终点（含），ISO datetime"
    ),
) -> analytics_service.AnalyticsWindow:
    try:
        return analytics_service.resolve_window(start_from, start_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/overview")
async def overview(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.overview(session, window))


@router.get("/latency")
async def latency(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.latency(session, window))


@router.get("/tokens")
async def tokens(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.tokens(session, window))


@router.get("/cost")
async def cost(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.cost(session, window))


@router.get("/agents")
async def agents(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.agents(session, window))


@router.get("/tools")
async def tools(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.tools(session, window))


@router.get("/models")
async def models(
    window: analytics_service.AnalyticsWindow = Depends(resolved_window),
    _user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await analytics_service.models(session, window))
