from typing import Generic, TypeVar

from pydantic import BaseModel

from app.core.middleware import request_id_var
from app.core.pagination import PageParams, PageResult

__all__ = ["ApiResponse", "PageParams", "PageResult", "ok"]

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: T | None = None
    trace_id: str | None = None


def ok(data=None) -> dict:
    return {"code": 0, "message": "ok", "data": data, "trace_id": request_id_var.get()}
