"""工具目录路由：登录即可读（viewer 允许），写操作留给后续任务。"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.user import User
from app.schemas.common import ok
from app.tools.registry import get_default_registry

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("")
async def list_tools(_: User = Depends(get_current_user)) -> dict:
    """列出当前进程已注册的工具（内置 9 个；MCP 工具在 Task 12 同步）。"""
    specs = get_default_registry().list_tools()
    items = [spec.public_dict() for spec in specs]
    return ok({"items": items, "total": len(items)})
