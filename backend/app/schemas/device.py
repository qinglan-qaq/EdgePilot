"""设备 API 请求/响应模型；输入 ``extra=forbid``。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DeviceKind = Literal["camera", "sensor", "other"]
DeviceStatus = Literal["online", "offline", "disabled"]


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int
    name: str
    kind: str
    config: dict
    status: str
    last_seen_at: datetime | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeviceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    kind: DeviceKind = "camera"
    config: dict = Field(default_factory=dict)
    status: DeviceStatus = "offline"


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    kind: DeviceKind | None = None
    config: dict | None = None
    status: DeviceStatus | None = None


class DeviceProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    traceparent: str | None = Field(
        default=None, pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"
    )


class DeviceProbeOut(BaseModel):
    accepted: bool
    device_id: int
    node_key: str
    task_id: str
    traceparent: str
    topic: str
