"""用户管理请求 / 响应模型。

邮箱一律在入库前 strip + 小写，这里用轻量正则校验，不引入 email-validator
依赖。响应模型绝不包含 ``password_hash``。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

UserRole = Literal["admin", "operator", "viewer"]
UserStatus = Literal["active", "disabled"]

# 只挡明显非法的值：要求本地部分/域名非空且有一个点，不追求完整 RFC。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("邮箱格式不正确")
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 255 or _EMAIL_RE.fullmatch(normalized) is None:
        raise ValueError("邮箱格式不正确")
    return normalized


class UserCreate(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    role: UserRole = "viewer"
    status: UserStatus = "active"

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> str:
        return _normalize_email(value)


class UserUpdate(BaseModel):
    email: str | None = Field(default=None, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: UserRole | None = None
    status: UserStatus | None = None

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> str | None:
        # 未传 email 时是 None；显式传 null 也保持可选语义。
        if value is None:
            return None
        return _normalize_email(value)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    display_name: str
    role: str
    status: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
