"""工作流与运行请求 / 响应模型。

草稿允许暂时非法，因此请求体里的 ``dsl`` 只要求是 JSON 对象；结构合法性
交给 ``app.workflow.validator``，不在 schema 层重复。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TriggerType = Literal["manual", "schedule", "event"]


# ---------------------------------------------------------------- 请求模型


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    dsl: dict | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    dsl: dict | None = None


class WorkflowValidateIn(BaseModel):
    """可选覆盖草稿的 DSL；不传则校验草稿。"""

    dsl: dict | None = None


class WorkflowRunCreate(BaseModel):
    input: dict | None = None
    trigger_type: TriggerType = "manual"
    trigger_payload: dict | None = None


class ApprovalDecisionIn(BaseModel):
    """审批意见；角色成员资格由 service 校验，schema 只限制长度。"""

    comment: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------- 响应模型


class RunSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    created_at: datetime | None = None


class WorkflowOut(BaseModel):
    """工作流详情；``dsl`` 优先草稿（编辑器视角）。"""

    id: int
    name: str
    description: str | None
    status: str
    current_version: int
    draft_version: int | None
    dsl: dict
    created_by: int | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_run: RunSummaryOut | None = None


class WorkflowListItem(BaseModel):
    id: int
    name: str
    description: str | None
    status: str
    current_version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_run: RunSummaryOut | None = None


class WorkflowVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workflow_id: int
    version: int
    dsl: dict
    dsl_hash: str
    created_by: int | None
    created_at: datetime | None = None


class WorkflowNodeRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    node_id: str
    node_type: str
    status: str
    attempt: int
    input: dict | None
    output: Any | None
    approval: dict | None
    error: str | None
    span_id: str | None = None
    heartbeat_at: datetime | None = None
    started_at: datetime | None
    finished_at: datetime | None


class WorkflowRunOut(BaseModel):
    id: int
    workflow_id: int
    workflow_name: str
    workflow_version_id: int
    workflow_version: int
    status: str
    trigger_type: str
    trigger_payload: dict | None
    input: dict | None
    output: Any | None
    error: str | None
    trace_id: str | None
    trace_available: bool = False
    root_span_id: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowRunDetailOut(WorkflowRunOut):
    nodes: list[WorkflowNodeRunOut] = Field(default_factory=list)


class RunLogOut(BaseModel):
    node_id: str
    node_type: str
    status: str
    attempt: int
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
