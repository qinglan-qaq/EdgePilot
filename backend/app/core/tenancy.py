import uuid
from contextvars import ContextVar, Token


class TenantNotSetError(RuntimeError):
    """请求链路缺少租户上下文；宁可报错也不能漏过滤。"""


_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("tenant_id", default=None)


def set_tenant_id(value: uuid.UUID) -> Token:
    return _tenant_id.set(value)


def reset_tenant_id(token: Token) -> None:
    _tenant_id.reset(token)


def current_tenant_id_or_none() -> uuid.UUID | None:
    return _tenant_id.get()


def get_tenant_id() -> uuid.UUID:
    value = _tenant_id.get()
    if value is None:
        raise TenantNotSetError("tenant_id 未设置：请求链路缺少租户上下文")
    return value
