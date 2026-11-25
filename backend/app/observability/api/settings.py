"""可观测设置端点：只读，仅管理员可访问。

采集模式由环境变量 ``OBS_CAPTURE_MODE`` 控制，本期没有持久化写入接口；
前端只能展示真实配置，并在“修改”时提示更新环境变量并重启。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.db import get_db
from app.observability.services import settings_service
from app.schemas.common import ok

router = APIRouter(prefix="/observability/settings", tags=["observability"])


@router.get("")
async def get_settings(
    _user=Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await settings_service.get_settings_view(session))
