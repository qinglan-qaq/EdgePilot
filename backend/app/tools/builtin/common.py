"""内置工具的公共设施：租户/会话解析、空结果与序列化。"""

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.tenancy import current_tenant_id_or_none
from app.db import get_session_factory
from app.tools.registry import ToolContext


@dataclass
class QueryScope:
    """一次内置查询的数据范围。

    ``session`` 为 None 表示没有可用的 DB 上下文（ctx=None 或缺租户），
    此时工具必须返回带 ``note`` 的空结果，绝不触库、绝不编数据。
    """

    session: Any | None
    tenant_id: uuid.UUID | None
    note: str | None = None


@asynccontextmanager
async def query_scope(ctx: ToolContext | None):
    """解析会话与租户。

    - ``ctx is None``：无 Agent 绑定，直接返回空 scope（不建连接）；
    - 有 ``ctx.session``：复用调用方会话（由调用方负责提交/回滚）；
    - 有 ``ctx.session_factory``：自建会话，用完关闭；
    - 都没有但有租户：回退进程级 session factory。
    """
    if ctx is None:
        yield QueryScope(None, None, "未提供工具上下文（ctx=None），未查询数据库")
        return

    tenant_id = ctx.tenant_id or current_tenant_id_or_none()
    if tenant_id is None:
        yield QueryScope(None, None, "缺少租户上下文，未查询数据库")
        return

    if ctx.session is not None:
        yield QueryScope(ctx.session, tenant_id)
        return

    factory = ctx.session_factory or get_session_factory()
    async with factory() as session:
        yield QueryScope(session, tenant_id)


def empty_payload(note: str | None = None, **extra: Any) -> dict:
    """空结果的标准形状：``{"items": [], "count": 0, "note"?: ...}``。"""
    payload: dict[str, Any] = {"items": [], "count": 0}
    if note:
        payload["note"] = note
    payload.update(extra)
    return payload


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def to_jsonable(value: Any) -> Any:
    """把 Decimal / datetime 转成 JSON 友好类型，其余原样返回。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value
