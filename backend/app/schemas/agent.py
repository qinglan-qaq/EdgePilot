"""Agent 请求 / 响应模型。

``agent_type`` 固定为六个角色；``config`` 默认空对象。响应模型只回显配置
引用与状态，不含任何密钥类字段（模型密钥在 ``model_configs`` 中单独存放）。
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AgentType = Literal["supervisor", "device", "vision", "diagnosis", "operations", "report"]

# 字段名含 model_config_id，关闭 Pydantic 的 model_ 保护命名空间告警。
_BASE_CONFIG = ConfigDict(protected_namespaces=())


class AgentCreate(BaseModel):
    model_config = _BASE_CONFIG

    name: str = Field(min_length=1, max_length=128)
    agent_type: AgentType
    model_config_id: int | None = Field(default=None, ge=1)
    prompt_template_id: int | None = Field(default=None, ge=1)
    prompt_version: int | None = Field(default=None, ge=1)
    config: dict = Field(default_factory=dict)
    status: Literal["active", "disabled"] = "active"
    tool_ids: list[int] | None = None


class AgentUpdate(BaseModel):
    model_config = _BASE_CONFIG

    name: str | None = Field(default=None, min_length=1, max_length=128)
    agent_type: AgentType | None = None
    model_config_id: int | None = Field(default=None, ge=1)
    prompt_template_id: int | None = Field(default=None, ge=1)
    prompt_version: int | None = Field(default=None, ge=1)
    config: dict | None = None
    status: Literal["active", "disabled"] | None = None
    tool_ids: list[int] | None = None


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    name: str
    agent_type: str
    model_config_id: int | None
    prompt_template_id: int | None
    prompt_version: int | None
    config: dict
    status: str
    current_version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    agent_id: int
    version: int
    snapshot: dict
    created_by: int | None
    created_at: datetime | None = None


class AgentRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    agent_id: int
    agent_version: int | None
    workflow_run_id: int | None
    node_run_id: int | None
    status: str
    input: dict | None
    output: dict | None
    error: str | None
    trace_id: str | None
    span_id: str | None
    total_tokens: int | None
    total_cost: Decimal | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime | None = None
