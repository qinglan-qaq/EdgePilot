"""单 Agent 试跑：预算/超时控制、结构化输出校验与 ``agent_runs`` 落库。

本模块只负责「跑一次 Agent」，不处理 HTTP 与鉴权：

* ``task`` 原样落库，绝不改写；
* Prompt 由 ``agents.registry`` 按 ``agent_type`` 选择：优先
  ``agent.config["system_prompt"]``，其次 ``backend/agents/prompts/{type}.md``，
  最后通用兜底；span 的 ``metadata.prompt_source`` 仍标注 ``inline_fallback``；
* 输出 Schema 按 ``agent_type`` 从 ``agents.schemas`` 取，必须通过 Pydantic
  校验；失败重试**恰好一次**，再失败即 ``agent_output_invalid``；
* 任何 LLM 错误都收敛成 ``status="failed"``，不伪造输出、不估算 token/成本。

``OBS_ENABLED=false`` 时 tracer 句柄是 no-op 对象，``trace_id`` / ``span_id``
为空串；落库前统一转 ``None``（列宽分别只有 32 / 16，空串会破坏语义）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.llm import LLMClient, LLMResponse
from app.agents.registry import get_build_messages
from app.agents.schemas import (
    AgentEvidence,
    AgentOutput,
    output_schema_for,
    parse_output,
)
from app.core.tenancy import get_tenant_id
from app.models.agent import Agent
from app.repositories.agent_repo import AgentRunRepo

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS_BUDGET = 60000
DEFAULT_TIMEOUT_S = 60.0

# 兼容旧引用：规范定义已迁移到 ``app.agents.schemas``。
__all__ = [
    "AgentEvidence",
    "AgentOutput",
    "AgentRunResult",
    "DEFAULT_MAX_TOKENS_BUDGET",
    "DEFAULT_TIMEOUT_S",
    "run_agent",
]

_LLM_ERROR_MESSAGES = {
    "timeout": "LLM 调用超时",
    "provider_error": "LLM 提供方调用失败",
    "missing_api_key": "缺少 API key",
    "mock_fixture_missing": "mock fixture 未命中",
}
_RETRYABLE_LLM_CODES = frozenset({"timeout", "provider_error"})
_OUTPUT_SCHEMA_INVALID = "output_schema_invalid"


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


@asynccontextmanager
async def _trace_scope(
    tracer: Any, name: str, existing: Any | None
) -> AsyncIterator[Any]:
    """复用外部 trace 句柄，或按原逻辑新开一条 trace。

    外部句柄（含 ``NullTrace``）由所有者管理生命周期，这里只借用其
    ``trace_id``，不设置 output、不关闭。
    """
    if existing is not None:
        yield existing
        return
    async with tracer.start_trace(name, source="agent_test") as trace:
        yield trace


@asynccontextmanager
async def _span_scope(
    tracer: Any,
    name: str,
    existing: Any | None,
    *,
    agent_id: int | None,
    agent_version: int | None,
) -> AsyncIterator[Any]:
    """复用外部 span 句柄，或新开一个由本函数收口的 agent span。

    外部句柄（含 ``NullSpan``）由所有者 finish；本函数不进入其上下文
    管理器，避免提前关闭。
    """
    if existing is not None:
        yield existing
        return
    async with tracer.start_span(
        name, "agent", agent_id=agent_id, agent_version=agent_version
    ) as span:
        yield span


@asynccontextmanager
async def _db_gate(lock: asyncio.Lock | None) -> AsyncIterator[None]:
    """串行化共享会话上的 DB 操作；``None`` 时保持原行为（不加锁）。

    Supervisor 的多个 ``Send`` 分支复用同一个请求级 ``AsyncSession``；
    SQLAlchemy 会话并非并发安全，加载 Agent / 写运行行的阶段必须排队。
    LLM 调用阶段不在此门内，因此分支仍然并行。
    """
    if lock is None:
        yield
        return
    async with lock:
        yield


async def run_agent(
    agent: Agent,
    task: dict,
    *,
    tracer,
    llm: LLMClient,
    session: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    trace_handle: Any | None = None,
    span_handle: Any | None = None,
    db_lock: asyncio.Lock | None = None,
) -> AgentRunResult:
    """执行一次 Agent 并写入一行 ``agent_runs``。

    Args:
        agent: 已发布的 Agent；``current_version`` 会写入运行行（可为 None）。
        task: 任务参数，原样落库并序列化为 user message。
        tracer: 观测 tracer；由调用方每次请求现取，避免缓存开关。
        llm: 已按请求构造的 ``LLMClient``（mock / real 由调用方决定）。
        session: 请求级会话；本函数提交一次运行行。
        tenant_id: 缺省取上下文租户；显式传入便于后台任务复用。
        trace_handle: 可选的既有 trace 句柄。传入时**不再新开 trace**，
            仅用其 ``trace_id`` 关联运行行，span 仍写在当前上下文里；
            默认 ``None`` 保持独立 trace 的原行为。
        span_handle: 可选的既有 span 句柄（通常由编排器创建）。传入时
            **不再新开 agent span，也不负责收口**，只写入 input/output/
            error/meta，由句柄所有者 finish；默认 ``None`` 时自行开 span
            并在退出时关闭。
        db_lock: 可选的会话互斥锁。Supervisor 的并行分支共享同一
            ``AsyncSession`` 时传入，保证「写运行行 + commit」串行执行；
            默认 ``None`` 时行为不变（单 Agent 试跑不加锁）。

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

    async with _trace_scope(
        tracer, f"agent_test.{agent.name}", trace_handle
    ) as trace:
        trace_id = _id_or_none(getattr(trace, "trace_id", None))
        async with _span_scope(
            tracer,
            f"agent.{agent.agent_type}",
            span_handle,
            agent_id=agent.id,
            agent_version=agent.current_version,
        ) as span:
            span_id = _id_or_none(getattr(span, "span_id", None))
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
            if outcome.status == "failed" and span_handle is None:
                # 显式收口：正常退出时 __aexit__ 会默认 succeeded。
                # 外部句柄由其所有者 finish，这里不能抢先关闭。
                span.finish("failed")
        if trace_handle is None:
            trace.set_output({"status": outcome.status, "error": outcome.error})

    finished_at = datetime.now(timezone.utc)
    async with _db_gate(db_lock):
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
    messages = _build_messages(agent, task)
    response_schema = output_schema_for(agent.agent_type)

    response = await llm.chat(
        messages, response_schema=response_schema, temperature=0
    )
    meter.add(response)

    terminal = _terminal_llm_error(response, retry_count=meter.retry_count)
    if terminal is not None:
        return terminal
    over_budget = _budget_outcome(meter, budget, retry_count=meter.retry_count)
    if over_budget is not None:
        return over_budget

    parsed, validation_error = _parse_output(agent.agent_type, response)
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
        retry_messages, response_schema=response_schema, temperature=0
    )
    meter.add(retry_response)

    terminal = _terminal_llm_error(retry_response, retry_count=meter.retry_count)
    if terminal is not None:
        return terminal
    over_budget = _budget_outcome(meter, budget, retry_count=meter.retry_count)
    if over_budget is not None:
        return over_budget

    parsed, retry_error = _parse_output(agent.agent_type, retry_response)
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


def _build_messages(agent: Agent, task: dict) -> list[dict]:
    """按 ``agent_type`` 从 registry 取消息构造器；未知类型在 registry 内回退。"""
    return get_build_messages(agent.agent_type)(agent, task)


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


def _parse_output(agent_type: str, response: LLMResponse) -> tuple[dict | None, str | None]:
    """解析并按角色模型校验；返回 ``(output, error)``，二者互斥。"""
    candidate = _extract_json(response)
    if candidate is None:
        return None, "内容不是合法的 JSON 对象"
    try:
        parsed = parse_output(agent_type, candidate)
    except ValidationError as exc:
        return None, _format_validation_error(exc)
    return parsed, None


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
