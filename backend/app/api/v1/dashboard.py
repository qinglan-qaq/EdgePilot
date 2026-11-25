"""Dashboard 汇总端点。

只读接口：所有登录角色（viewer / operator / admin）均可读取。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.services.dashboard_service import get_summary

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(
    hours: int = Query(24, ge=1, le=168),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    payload = await get_summary(session, hours)
    return ok(payload)
