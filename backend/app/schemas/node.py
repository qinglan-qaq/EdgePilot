"""节点 API 请求/响应模型。

时间字段由 FastAPI 统一编码为 ISO 8601。输入模型 ``extra=forbid``，
未知字段直接 422。禁用状态持久化在 ``labels["disabled"]``，不新增列。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CommandAction = Literal["start_task", "stop_task", "probe_device", "sync_config"]

TRACEPARENT_PATTERN = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"


class NodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_key: str
    name: str
    os: str | None
    arch: str | None
    agent_version: str | None
    status: str
    ip: str | None
    labels: dict
    metrics: dict | None
    last_heartbeat_at: datetime | None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NodeMetricsOut(BaseModel):
    node_key: str
    status: str
    metrics: dict | None
    last_heartbeat_at: datetime | None


class NodeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=128)
    labels: dict | None = None
    disabled: bool | None = None


class NodeCommandIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: CommandAction
    task_id: str | None = Field(default=None, min_length=1, max_length=64)
    payload: dict | None = None
    traceparent: str | None = Field(default=None, pattern=TRACEPARENT_PATTERN)


class NodeCommandOut(BaseModel):
    accepted: bool
    node_key: str
    action: str
    task_id: str
    traceparent: str
    topic: str
