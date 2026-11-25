"""观测 API 的响应模型。"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TraceSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trace_id: str
    name: str
    source: str
    status: str
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float | None = None
    total_spans: int = 0
    error_count: int = 0
    total_tokens: int | None = None
    total_cost: Decimal | None = None


class SpanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    span_id: str
    trace_id: str
    parent_span_id: str | None = None
    name: str
    span_type: str
    status: str
    attempt: int = 1
    agent_id: int | None = None
    agent_version: int | None = None
    workflow_id: int | None = None
    node_id: str | None = None
    tool_id: str | None = None
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float | None = None
    input: dict | None = None
    output: dict | None = None
    error: dict | None = None
    retry_count: int = 0
    meta: dict | None = None
    links: list | dict | None = None


class SpanDetail(SpanOut):
    llm_calls: list[dict] = Field(default_factory=list)
    tool_calls: list[dict] = Field(default_factory=list)
    retrievals: list[dict] = Field(default_factory=list)
