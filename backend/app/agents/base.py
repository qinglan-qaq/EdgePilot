"""单 Agent 试跑：预算/超时控制、结构化输出校验与 ``agent_runs`` 落库。

本模块只负责「跑一次 Agent」，不处理 HTTP 与鉴权：

* ``task`` 原样落库，绝不改写；
* Prompt 模板快照渲染尚未接入（Task 13+），本期使用
  ``agent.config["system_prompt"]`` 内联回退，并在 span 的
  ``metadata.prompt_source`` 上标注 ``inline_fallback``；
* 输出必须通过 Pydantic 校验；失败重试**恰好一次**，再失败即
  ``agent_output_invalid``；
* 任何 LLM 错误都收敛成 ``status="failed"``，不伪造输出、不估算 token/成本。

``OBS_ENABLED=false`` 时 tracer 句柄是 no-op 对象，``trace_id`` / ``span_id``
为空串；落库前统一转 ``None``（列宽分别只有 32 / 16，空串会破坏语义）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import LLMClient, LLMResponse
from app.core.tenancy import get_tenant_id
from app.models.agent import Agent
from app.repositories.agent_repo import AgentRunRepo

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS_BUDGET = 60000
DEFAULT_TIMEOUT_S = 60.0

# 本阶段单 Agent 的固定输出 Schema；Task 13 会按 agent_type 拆分到 agents/schemas.py。
DEVICE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer", "evidence"],
    "properties": {
        "answer": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "ref"],
                "properties": {
                    "type": {"type": "string"},
                    "ref": {"type": "string"},
                },
            },
        },
    },
}

_LLM_ERROR_MESSAGES = {
    "timeout": "LLM 调用超时",
    "provider_error": "LLM 提供方调用失败",
    "missing_api_key": "缺少 API key",
    "mock_fixture_missing": "mock fixture 未命中",
}
_RETRYABLE_LLM_CODES = frozenset({"timeout", "provider_error"})
_OUTPUT_SCHEMA_INVALID = "output_schema_invalid"


class AgentEvidence(BaseModel):
    """一条可回溯证据；本期只约束 ``type`` / ``ref`` 两个字段。"""

    model_config = ConfigDict(extra="allow")

    type: str
    ref: str


class AgentOutput(BaseModel):
    """单 Agent 的结构化输出；结论必须带证据（Task 13 再细分领域模型）。"""

    model_config = ConfigDict(extra="allow")

    answer: str
    evidence: list[AgentEvidence]


@dataclass
class AgentRunResult:
    """``run_agent`` 的返回值；字段与 ``agent_runs`` 行一一对应。"""

    agent_run_id: int
    status: str
    output: dict | None
    error: str | None
    trace_id: str | None
    span_id: str | None
    total_tokens: int | None
    total_cost: Decimal | None
    retry_count: int


@dataclass
class _Outcome:
    """内部执行结果；额外携带 span 错误标注所需的信息。"""

    status: str
    output: dict | None
    error: str | None
    retry_count: int
    span_error_code: str | None = None
    span_error_message: str | None = None
    span_error_retryable: bool = False


class _UsageMeter:
    """累计每次 LLM 调用实际回报的 token / 成本；缺失即不计，绝不估算。

    超时取消后仍可通过本对象回读已完成调用的用量。
    """

    def __init__(self) -> None:
        self.total_tokens: int | None = None
        self.total_cost: Decimal | None = None
        self.retry_count = 0

    def add(self, response: LLMResponse) -> None:
        usage = response.usage if isinstance(response.usage, dict) else {}
        tokens = usage.get("total_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            self.total_tokens = (self.total_tokens or 0) + tokens
        if isinstance(response.cost, Decimal):
            self.total_cost = (self.total_cost or Decimal(0)) + response.cost


async def run_agent(
    agent: Agent,
    task: dict,
    *,
    tracer,
    llm: LLMClient,
    session: AsyncSession,
    tenant_id: uuid.UUID | None = None,
) -> AgentRunResult:
    """执行一次 Agent 并写入一行 ``agent_runs``。

    Args:
        agent: 已发布的 Agent；``current_version`` 会写入运行行（可为 None）。
        task: 任务参数，原样落库并序列化为 user message。
        tracer: 观测 tracer；由调用方每次请求现取，避免缓存开关。
        llm: 已按请求构造的 ``LLMClient``（mock / real 由调用方决定）。
        session: 请求级会话；本函数提交一次运行行。
        tenant_id: 缺省取上下文租户；显式传入便于后台任务复用。

    Returns:
        ``AgentRunResult``；失败也返回结果，不抛业务异常。
    """
    tenant = tenant_id or get_tenant_id()
    config = agent.config if isinstance(agent.config, dict) else {}
    budget = _positive_int(config.get("max_tokens_budget"), DEFAULT_MAX_TOKENS_BUDGET)
    timeout_s = _positive_float(config.get("timeout_s"), DEFAULT_TIMEOUT_S)

    started_at = datetime.now(timezone.utc)
    meter = _UsageMeter()
    trace_id: str | None = None
    span_id: str | None = None
    outcome = _Outcome("failed", None, "internal_error: 未执行", 0)

    async with tracer.start_trace(f"agent_test.{agent.name}", source="agent_test") as trace:
        trace_id = _id_or_none(trace.trace_id)
        async with tracer.start_span(
            f"agent.{agent.agent_type}",
            "agent",
            agent_id=agent.id,
            agent_version=agent.current_version,
        ) as span:
            span_id = _id_or_none(span.span_id)
            span.set_input(task).set_meta(prompt_source="inline_fallback")
            try:
                outcome = await asyncio.wait_for(
                    _execute(agent, task, llm=llm, meter=meter, budget=budget),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                message = f"运行超过 {timeout_s:g}s 未完成"
                outcome = _Outcome(
                    "failed", None, f"timeout: {message}", meter.retry_count,
                    "timeout", message, True,
                )
            except Exception as exc:  # noqa: BLE001 - 失败必须落库为 200，不升级为 500
                logger.exception("Agent 试跑异常（agent_id=%s）", agent.id)
                message = f"{type(exc).__name__}: {exc}"
                outcome = _Outcome(
                    "failed", None, f"internal_error: {message}", meter.retry_count,
                    "internal_error", message, False,
                )

            if outcome.output is not None:
                span.set_output(outcome.output)
            if outcome.span_error_code:
                span.set_error(
                    outcome.span_error_code,
                    outcome.span_error_message or "",
                    retryable=outcome.span_error_retryable,
                )
            if outcome.status == "failed":
                # 显式收口：正常退出时 __aexit__ 会默认 succeeded。
                span.finish("failed")
        trace.set_output({"status": outcome.status, "error": outcome.error})

    finished_at = datetime.now(timezone.utc)
    row = await AgentRunRepo(session).create_run(
        tenant_id=tenant,
        agent_id=agent.id,
        agent_version=agent.current_version,
        status=outcome.status,
        input=task,
        output=outcome.output,
        error=outcome.error,
        span_id=span_id,
        trace_id=trace_id,
        total_tokens=meter.total_tokens,
        total_cost=meter.total_cost,
        retry_count=outcome.retry_count,
        started_at=started_at,
        finished_at=finished_at,
    )
    await session.commit()

    return AgentRunResult(
        agent_run_id=row.id,
        status=outcome.status,
        output=outcome.output,
        error=outcome.error,
        trace_id=trace_id,
        span_id=span_id,
        total_tokens=meter.total_tokens,
        total_cost=meter.total_cost,
        retry_count=outcome.retry_count,
    )


async def _execute(
    agent: Agent,
    task: dict,
    *,
    llm: LLMClient,
    meter: _UsageMeter,
    budget: int,
) -> _Outcome:
    """调用 LLM 并校验输出；Schema 失败时重试一次。"""
    config = agent.config if isinstance(agent.config, dict) else {}
    messages = _build_messages(config, task)

    response = await llm.chat(
        messages, response_schema=DEVICE_OUTPUT_SCHEMA, temperature=0
    )
    meter.add(response)

    terminal = _terminal_llm_error(response, retry_count=meter.retry_count)
    if terminal is not None:
        return terminal
    over_budget = _budget_outcome(meter, budget, retry_count=meter.retry_count)
    if over_budget is not None:
        return over_budget

    parsed, validation_error = _parse_output(response)
    if parsed is not None:
        return _Outcome("succeeded", parsed, None, meter.retry_count)

    # 第一次校验失败：附带纠错说明后重试一次，之后无论成败都不再重试。
    meter.retry_count = 1
    retry_messages = messages + [
        {
            "role": "user",
            "content": (
                f"上一次输出未通过 JSON Schema 校验：{validation_error}。"
                "请只返回符合 schema 的 JSON 对象，不要包含解释或 Markdown 代码块。"
            ),
        }
    ]
    retry_response = await llm.chat(
        retry_messages, response_schema=DEVICE_OUTPUT_SCHEMA, temperature=0
    )
    meter.add(retry_response)

    terminal = _terminal_llm_error(retry_response, retry_count=meter.retry_count)
    if terminal is not None:
        return terminal
    over_budget = _budget_outcome(meter, budget, retry_count=meter.retry_count)
    if over_budget is not None:
        return over_budget

    parsed, retry_error = _parse_output(retry_response)
    if parsed is not None:
        return _Outcome("succeeded", parsed, None, meter.retry_count)

    message = f"输出未通过 Schema 校验（已重试 1 次）：{retry_error}"
    return _Outcome(
        "failed",
        _extract_json(retry_response),
        f"agent_output_invalid: {message}",
        meter.retry_count,
        "agent_output_invalid",
        message,
        False,
    )


def _build_messages(config: dict, task: dict) -> list[dict]:
    """内联 system prompt + JSON 序列化的用户任务。"""
    messages: list[dict] = []
    system_prompt = config.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append(
        {"role": "user", "content": json.dumps(task, ensure_ascii=False, default=str)}
    )
    return messages


def _terminal_llm_error(response: LLMResponse, *, retry_count: int) -> _Outcome | None:
    """把 LLM 的终态错误码转成失败结果；``output_schema_invalid`` 交给校验重试路径。"""
    code = response.error_code
    if not code or code == _OUTPUT_SCHEMA_INVALID:
        return None
    message = _LLM_ERROR_MESSAGES.get(code, "LLM 调用失败")
    return _Outcome(
        "failed", None, f"{code}: {message}", retry_count,
        code, message, code in _RETRYABLE_LLM_CODES,
    )


def _budget_outcome(meter: _UsageMeter, budget: int, *, retry_count: int) -> _Outcome | None:
    """token 用量超过预算即失败；未回报 usage 时不做估算、直接放行。"""
    if meter.total_tokens is None or meter.total_tokens <= budget:
        return None
    message = f"累计 token 用量 {meter.total_tokens} 超过预算 {budget}"
    return _Outcome(
        "failed", None, f"budget_exceeded: {message}", retry_count,
        "budget_exceeded", message, False,
    )


def _parse_output(response: LLMResponse) -> tuple[dict | None, str | None]:
    """解析并 Pydantic 校验；返回 ``(output, error)``，二者互斥。"""
    candidate = _extract_json(response)
    if candidate is None:
        return None, "内容不是合法的 JSON 对象"
    try:
        model = AgentOutput.model_validate(candidate)
    except ValidationError as exc:
        return None, _format_validation_error(exc)
    return model.model_dump(), None


def _extract_json(response: LLMResponse) -> dict | None:
    """优先解析 ``content`` 字符串；非 JSON 时回退到 ``structured``。"""
    content = response.content if isinstance(response.content, str) else ""
    if content.strip():
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
    if isinstance(response.structured, dict):
        return response.structured
    return None


def _format_validation_error(exc: ValidationError) -> str:
    """压缩 Pydantic 校验错误，最多保留前 3 条避免写爆 ``error`` 列。"""
    parts: list[str] = []
    for item in exc.errors()[:3]:
        loc = ".".join(str(part) for part in item.get("loc", ()))
        parts.append(f"{loc}: {item.get('msg', 'invalid')}")
    return "; ".join(parts) or "未知校验错误"


def _positive_int(value: Any, default: int) -> int:
    """只接受真正的正整数；其余回落默认值，不让配置错误打断运行。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _positive_float(value: Any, default: float) -> float:
    """只接受正的数值型超时（秒）；其余回落默认值。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


def _id_or_none(value: Any) -> str | None:
    """观测句柄关闭时 id 可能是空串，落库前统一转 ``None``。"""
    if isinstance(value, str) and value:
        return value
    return None
