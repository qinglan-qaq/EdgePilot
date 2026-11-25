"""Supervisor Agent：Plan-and-Execute 编排 + LangGraph ``Send`` 扇出。

职责边界（MVP 设计 §8.1）：

* Supervisor **不直接调用任何数据工具**，只能把子任务派发给六个领域角色；
  允许派发的类型在代码里白名单化，``tool`` / ``supervisor`` 等越权类型在
  **派发前**即被拒绝，返回 ``supervisor_invalid_assignment``；
* 硬上限（``SupervisorLimits``）：≤6 次 Agent 调用、≤2 次重规划、60k token、
  60s。调用上限在 ``plan`` 节点切片，token 上限在 ``assess`` 节点判定，
  超时由 ``asyncio.wait_for`` 兜底——都不是靠 prompt 自觉；
* 汇总不编造事实：子 Agent 输出逐条原样保留，``evidence`` 原样合并，
  ``summary`` 只是确定性的结构化计数；
* 任何异常都收敛为 ``failed`` / ``partial`` 结果，调用方看不到裸异常。

图结构::

    START → plan ─┬─ Send("agent", {assignment}) × N → assess
                  └─ assess（无可派发任务）
    assess ─┬─ 失败且预算允许 → plan（重规划）
            └─ finalize → END

并行分支只返回 ``{"results": [entry]}`` / ``{"errors": [...]}``，由
``operator.add`` reducer 合并，避免 LangGraph 并发写入丢结果；超时后
从共享的 ``_RunBuffer`` 取已完成的部分结果。
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.prompts import build_messages as _build_messages, load_default_prompt, prompt_path
from app.agents.schemas import (
    Evidence,
    SupervisorOutput,
    output_schema_for,
    parse_output,
)

# ``Send`` 在 langgraph 1.x 位于 ``langgraph.types``（``langgraph.constants`` 已弃用）。
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

logger = logging.getLogger(__name__)

AGENT_TYPE = "supervisor"
OUTPUT_MODEL = SupervisorOutput
SYSTEM_PROMPT_PATH: Path = prompt_path(AGENT_TYPE)

#: Supervisor 只能派发给这些领域角色；数据工具与自调用永远不在其中。
DOMAIN_AGENT_TYPES: frozenset[str] = frozenset(
    {"device", "vision", "diagnosis", "operations", "report"}
)

DEFAULT_MAX_AGENT_CALLS = 6
DEFAULT_MAX_REPLANS = 2
DEFAULT_MAX_TOKENS = 60000
DEFAULT_TIMEOUT_S = 60.0

# 结构化错误码（写进 errors / error，调用方按前缀匹配）。
INVALID_ASSIGNMENT = "supervisor_invalid_assignment"
AGENT_CALL_LIMIT = "agent_call_limit"
BUDGET_EXCEEDED = "budget_exceeded"
TIMEOUT = "timeout"
PLANNER_UNAVAILABLE = "supervisor_planner_unavailable"
PLANNER_FAILED = "supervisor_planner_failed"
PLANNER_INVALID = "supervisor_planner_invalid"
EMPTY_PLAN = "supervisor_empty_plan"
AGENT_UNAVAILABLE = "supervisor_agent_unavailable"
RUNNER_UNAVAILABLE = "supervisor_runner_unavailable"
RUNNER_ERROR = "supervisor_runner_error"
INTERNAL_ERROR = "internal_error"

_STATUSES = frozenset({"succeeded", "partial", "failed"})


def build_messages(agent: Any, task: dict) -> list[dict]:
    """构造 Supervisor 的对话消息（system prompt 优先取 Agent 配置）。"""
    return _build_messages(agent, task)


# --------------------------------------------------------------------------- 契约


@dataclass(frozen=True)
class SupervisorLimits:
    """编排预算：调用次数 / 重规划次数 / token / 墙钟超时。"""

    max_agent_calls: int = DEFAULT_MAX_AGENT_CALLS
    max_replans: int = DEFAULT_MAX_REPLANS
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout_s: float = DEFAULT_TIMEOUT_S


class Assignment(BaseModel):
    """一条派发：只描述「哪个角色做什么」，不含工具调用。

    ``agent_type`` 此处不做白名单校验（planner 可能给出越权类型，
    需要由 ``Supervisor`` 统一转成 ``supervisor_invalid_assignment``）。
    """

    model_config = ConfigDict(extra="allow")

    agent_type: str
    goal: str
    input: dict[str, Any] = Field(default_factory=dict)


@dataclass
class SupervisorResult:
    """一次编排的完整结果；``output`` 为聚合后的 ``SupervisorOutput``。"""

    status: str
    output: SupervisorOutput | dict | None = None
    results: list[dict] = field(default_factory=list)
    agent_calls: int = 0
    replans: int = 0
    total_tokens: int = 0
    error: str | None = None
    errors: list[str] = field(default_factory=list)
    trace_id: str | None = None
    span_id: str | None = None


class SupervisorPlannerError(Exception):
    """planner 无法给出可执行计划；``code`` 会写入 ``SupervisorResult.errors``。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class SupervisorRunnerError(Exception):
    """默认 runner 无法加载/执行目标 Agent（结构性失败，不是子 Agent 业务失败）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class _RunBuffer:
    """图外可读的实时进度；超时/取消时用它返回已完成的部分结果。"""

    results: list[dict] = field(default_factory=list)
    replans: int = 0


class _GraphState(TypedDict, total=False):
    """LangGraph 状态；并行分支只写 reducer 字段。"""

    task: dict
    failed: list[dict]
    replans: int
    wave_start: int
    assignments: list[dict]
    assignment: dict
    results: Annotated[list[dict], operator.add]
    errors: Annotated[list[str], operator.add]
    dropped: Annotated[int, operator.add]
    status: str
    output: Any
    buffer: Any


PlanFn = Callable[..., Awaitable[Any]]
RunFn = Callable[[Assignment], Awaitable[Any]]
AgentLoader = Callable[[str], Awaitable[Any]]


# --------------------------------------------------------------------- Supervisor


class Supervisor:
    """Plan-and-Execute 编排器；planner / runner / tracer 全部可注入。

    Args:
        llm: 默认 planner 使用的 ``LLMClient``；为 ``None`` 时 planner 以
            ``supervisor_planner_unavailable`` 失败，绝不凭空编造计划。
        tracer: 可选 tracer。仅在已存在当前 trace 时挂一个 ``supervisor.run``
            span；``None`` 或观测关闭时完全 no-op。
        planner: ``async (task, *, failed, replans) -> list[Assignment]``。
        runner: ``async (assignment) -> AgentRunResult 兼容对象``。
        limits: 预算硬上限。
        agent_loader: ``async (agent_type) -> Agent | None``；默认路径用
            ``session`` + 租户上下文查已发布 Agent（P3 默认 runner 的一部分）。
        session: 默认 runner / agent_loader 所需的请求级会话。注入 runner
            时不需要（单测因此不碰数据库）。

    Note:
        ``session`` 是相对契约签名新增的可选参数，仅服务于「默认 runner
        解析已发布 Agent」；不传且未注入 runner 时抛结构化
        ``supervisor_runner_unavailable``。
    """

    def __init__(
        self,
        *,
        llm: Any | None = None,
        tracer: Any | None = None,
        planner: PlanFn | None = None,
        runner: RunFn | None = None,
        limits: SupervisorLimits | None = None,
        agent_loader: AgentLoader | None = None,
        session: Any | None = None,
    ) -> None:
        self._llm = llm
        self._tracer = tracer
        self._planner = planner or self._default_planner
        self._runner = runner or self._default_runner
        self._limits = limits or SupervisorLimits()
        self._agent_loader = agent_loader
        self._session = session
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ 入口

    async def run(self, task: dict) -> SupervisorResult:
        """执行一次编排；超时 / 图异常都返回结构化结果而非抛异常。"""
        buffer = _RunBuffer()
        span = self._begin_span(task)
        try:
            state = await asyncio.wait_for(
                self._graph.ainvoke(self._initial_state(task, buffer)),
                timeout=self._limits.timeout_s,
            )
            result = self._result_from_state(state)
        except asyncio.TimeoutError:
            message = f"{TIMEOUT}: 编排超过 {self._limits.timeout_s:g}s 未完成"
            result = self._result_from_buffer(buffer, [message], status=None)
        except Exception as exc:  # noqa: BLE001 - 调用方绝不应该看到裸异常
            logger.exception("Supervisor 编排异常")
            message = f"{INTERNAL_ERROR}: {type(exc).__name__}: {exc}"
            result = self._result_from_buffer(buffer, [message], status=None)

        if result.trace_id is None and span is not None:
            result.trace_id = _id_or_none(getattr(span, "trace_id", None))
        if result.span_id is None and span is not None:
            result.span_id = _id_or_none(getattr(span, "span_id", None))
        self._finish_span(span, result)
        return result

    # ------------------------------------------------------------------ 图节点

    def _build_graph(self):
        """编译 Plan-and-Execute 状态图；每次 ``run`` 复用同一编译结果。"""
        builder: StateGraph = StateGraph(_GraphState)
        builder.add_node("plan", self._plan_node)
        builder.add_node("agent", self._agent_node)
        builder.add_node("assess", self._assess_node)
        builder.add_node("finalize", self._finalize_node)
        builder.add_edge(START, "plan")
        builder.add_conditional_edges("plan", _route_after_plan, ["agent", "assess"])
        builder.add_edge("agent", "assess")
        builder.add_conditional_edges("assess", _route_after_assess, ["plan", "finalize"])
        builder.add_edge("finalize", END)
        return builder.compile()

    def _initial_state(self, task: dict, buffer: _RunBuffer) -> _GraphState:
        return _GraphState(
            task=dict(task),
            failed=[],
            replans=0,
            wave_start=0,
            assignments=[],
            assignment={},
            results=[],
            errors=[],
            dropped=0,
            output=None,
            buffer=buffer,
        )

    async def _plan_node(self, state: _GraphState) -> dict:
        """取计划 → 校验类型 → 按剩余调用预算切片 → 返回待派发列表。"""
        results = list(state.get("results") or [])
        replans = int(state.get("replans") or 0)
        failed = [
            Assignment.model_validate(item) for item in (state.get("failed") or [])
        ]

        try:
            planned = await self._planner(
                dict(state.get("task") or {}),
                failed=failed or None,
                replans=replans,
            )
        except SupervisorPlannerError as exc:
            return {"assignments": [], "failed": [], "errors": [str(exc)]}
        except Exception as exc:  # noqa: BLE001 - planner 异常收敛为结构化错误
            logger.exception("Supervisor planner 异常")
            return {
                "assignments": [],
                "failed": [],
                "errors": [f"{PLANNER_FAILED}: {type(exc).__name__}: {exc}"],
            }

        assignments, errors = _validate_assignments(planned)
        if not assignments and not errors:
            errors.append(f"{EMPTY_PLAN}: 规划结果为空，无可派发子任务")
        if not assignments:
            return {"assignments": [], "failed": [], "errors": errors}

        remaining = max(self._limits.max_agent_calls - len(results), 0)
        dispatched = assignments[:remaining]
        dropped = len(assignments) - len(dispatched)
        if dropped:
            errors.append(f"{AGENT_CALL_LIMIT}: 已丢弃 {dropped} 个超出上限的派发")

        return {
            "assignments": [item.model_dump() for item in dispatched],
            "failed": [],
            "wave_start": len(results),
            "errors": errors,
            "dropped": dropped,
        }

    async def _agent_node(self, state: _GraphState) -> dict:
        """执行单个派发；成功/失败都落一条结果，绝不把异常抛出图外。"""
        try:
            assignment = Assignment.model_validate(state.get("assignment") or {})
        except ValidationError as exc:
            entry = _failed_entry(
                agent_type=str((state.get("assignment") or {}).get("agent_type", "")),
                goal=str((state.get("assignment") or {}).get("goal", "")),
                input={},
                error=f"{INVALID_ASSIGNMENT}: 派发参数不合法：{_format_validation_error(exc)}",
            )
            return self._append_result(state, entry)

        try:
            run = await self._runner(assignment)
            entry = _entry_from_run(assignment, run)
        except SupervisorRunnerError as exc:
            entry = _failed_entry(
                agent_type=assignment.agent_type,
                goal=assignment.goal,
                input=assignment.input,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - runner 异常收敛为子任务失败
            logger.exception("Supervisor runner 异常（agent_type=%s）", assignment.agent_type)
            entry = _failed_entry(
                agent_type=assignment.agent_type,
                goal=assignment.goal,
                input=assignment.input,
                error=f"{RUNNER_ERROR}: {type(exc).__name__}: {exc}",
            )
        return self._append_result(state, entry)

    def _append_result(self, state: _GraphState, entry: dict) -> dict:
        buffer = state.get("buffer")
        if isinstance(buffer, _RunBuffer):
            buffer.results.append(entry)
        return {"results": [entry]}

    async def _assess_node(self, state: _GraphState) -> dict:
        """统计当前波次失败与累计 token；决定是否重规划。"""
        results = list(state.get("results") or [])
        wave_start = int(state.get("wave_start") or 0)
        wave = results[wave_start:]
        replans = int(state.get("replans") or 0)
        total_tokens = _sum_tokens(results)

        failed = [
            entry for entry in wave if entry.get("status") != "succeeded"
        ]
        errors: list[str] = []
        exceeded = total_tokens > self._limits.max_tokens
        if exceeded:
            message = (
                f"{BUDGET_EXCEEDED}: 累计 token 用量 {total_tokens} "
                f"超过预算 {self._limits.max_tokens}"
            )
            if message not in (state.get("errors") or []):
                errors.append(message)

        should_replan = (
            bool(failed) and replans < self._limits.max_replans and not exceeded
        )
        if should_replan:
            buffer = state.get("buffer")
            if isinstance(buffer, _RunBuffer):
                buffer.replans = replans + 1
            return {
                "failed": [_failed_assignment_dict(entry) for entry in failed],
                "replans": replans + 1,
                "errors": errors,
            }
        return {"failed": [], "replans": replans, "errors": errors}

    async def _finalize_node(self, state: _GraphState) -> dict:
        """聚合子结果：输出原样保留，summary 只做确定性计数。"""
        results = list(state.get("results") or [])
        errors = list(state.get("errors") or [])
        dropped = int(state.get("dropped") or 0)
        output = _aggregate_output(results, errors, dropped)
        return {"output": output, "status": _final_status(results, dropped, errors)}

    # ------------------------------------------------------------------ planner

    async def _default_planner(
        self,
        task: dict,
        *,
        failed: list[Assignment] | None,
        replans: int,
    ) -> list[Assignment]:
        """默认 planner：调用 Supervisor prompt 生成计划；无 LLM 时诚实失败。"""
        if self._llm is None:
            raise SupervisorPlannerError(
                PLANNER_UNAVAILABLE, "未注入 LLM，无法生成执行计划"
            )

        messages = _planner_messages(task, failed=failed, replans=replans)
        response = await self._llm.chat(
            messages, response_schema=output_schema_for(AGENT_TYPE), temperature=0
        )
        if response.error_code:
            raise SupervisorPlannerError(
                PLANNER_FAILED, f"LLM 返回错误 {response.error_code}"
            )

        candidate = _candidate_from_response(response)
        if candidate is None:
            raise SupervisorPlannerError(PLANNER_INVALID, "计划不是合法的 JSON 对象")
        try:
            parse_output(AGENT_TYPE, candidate)
        except ValidationError as exc:
            raise SupervisorPlannerError(
                PLANNER_INVALID, f"计划未通过 Schema 校验：{_format_validation_error(exc)}"
            ) from exc

        raw = candidate.get("assignments")
        if not isinstance(raw, list):
            raw = candidate.get("subtasks")
        if not isinstance(raw, list):
            raise SupervisorPlannerError(PLANNER_INVALID, "计划缺少 assignments 列表")
        return [_assignment_from_plan(item) for item in raw]

    # -------------------------------------------------------------------- runner

    async def _default_runner(self, assignment: Assignment) -> Any:
        """默认 runner：按 ``agent_type`` 取已发布 Agent，再调用 ``run_agent``。"""
        if self._session is None:
            raise SupervisorRunnerError(
                RUNNER_UNAVAILABLE, "默认 runner 需要 session；请在构造函数传入"
            )
        if self._llm is None:
            raise SupervisorRunnerError(
                RUNNER_UNAVAILABLE, "默认 runner 需要 llm；请在构造函数传入"
            )

        loader = self._agent_loader or self._load_published_agent
        try:
            agent = await loader(assignment.agent_type)
        except SupervisorRunnerError:
            raise
        except Exception as exc:  # noqa: BLE001 - 加载失败收敛为结构化错误
            logger.exception("加载 Agent 失败（agent_type=%s）", assignment.agent_type)
            raise SupervisorRunnerError(
                AGENT_UNAVAILABLE,
                f"加载 {assignment.agent_type} Agent 失败：{type(exc).__name__}",
            ) from exc
        if agent is None:
            raise SupervisorRunnerError(
                AGENT_UNAVAILABLE,
                f"没有已发布的 {assignment.agent_type} Agent",
            )

        from app.agents.base import run_agent
        from app.observability import get_tracer

        payload = {"goal": assignment.goal, "input": assignment.input}
        return await run_agent(
            agent,
            payload,
            tracer=self._tracer or get_tracer(),
            llm=self._llm,
            session=self._session,
        )

    async def _load_published_agent(self, agent_type: str) -> Any:
        """租户内取该角色最新发布的 Agent；没有则返回 ``None``。"""
        from sqlalchemy import select

        from app.core.tenancy import get_tenant_id
        from app.models.agent import Agent

        return await self._session.scalar(
            select(Agent)
            .where(
                Agent.tenant_id == get_tenant_id(),
                Agent.agent_type == agent_type,
                Agent.status == "active",
                Agent.current_version > 0,
            )
            .order_by(Agent.current_version.desc(), Agent.id.desc())
            .limit(1)
        )

    # -------------------------------------------------------------------- 结果

    def _result_from_state(self, state: _GraphState) -> SupervisorResult:
        results = list(state.get("results") or [])
        errors = list(state.get("errors") or [])
        output = state.get("output")
        status = str(state.get("status") or _final_status(
            results, int(state.get("dropped") or 0), errors
        ))
        if status not in _STATUSES:
            status = "partial"
        return SupervisorResult(
            status=status,
            output=output,
            results=results,
            agent_calls=len(results),
            replans=int(state.get("replans") or 0),
            total_tokens=_sum_tokens(results),
            error=_primary_error(errors, results),
            errors=errors,
        )

    def _result_from_buffer(
        self,
        buffer: _RunBuffer,
        errors: list[str],
        *,
        status: str | None,
    ) -> SupervisorResult:
        """超时 / 图异常：从共享缓冲返回已完成结果，绝不补造未完成项。"""
        results = list(buffer.results)
        return SupervisorResult(
            status=status or _final_status(results, 0, errors),
            output=_aggregate_output(results, errors, 0) if results else None,
            results=results,
            agent_calls=len(results),
            replans=buffer.replans,
            total_tokens=_sum_tokens(results),
            error=_primary_error(errors, results),
            errors=errors,
        )

    # -------------------------------------------------------------------- 观测

    def _begin_span(self, task: dict) -> Any | None:
        """有 tracer 且上下文存在 trace 时开一次 ``supervisor.run`` span。"""
        if self._tracer is None:
            return None
        try:
            span = self._tracer.start_span("supervisor.run", "agent")
            span.set_input(task)
            return span
        except Exception as exc:  # noqa: BLE001 - 观测是旁路能力
            logger.debug("Supervisor span 开启失败：%s", exc)
            return None

    def _finish_span(self, span: Any | None, result: SupervisorResult) -> None:
        if span is None:
            return
        try:
            span.set_output(
                {
                    "status": result.status,
                    "agent_calls": result.agent_calls,
                    "replans": result.replans,
                    "total_tokens": result.total_tokens,
                    "errors": result.errors,
                }
            )
            if result.status == "failed":
                span.set_error("supervisor_failed", result.error or "", retryable=False)
                span.finish("failed")
            else:
                span.finish("succeeded")
        except Exception as exc:  # noqa: BLE001 - 观测是旁路能力
            logger.debug("Supervisor span 收口失败：%s", exc)


# ------------------------------------------------------------------------ 路由


def _route_after_plan(state: _GraphState) -> list[Send] | str:
    """有待派发任务就 ``Send`` 扇出，否则直接进入评估（可能为空计划）。"""
    assignments = state.get("assignments") or []
    if not assignments:
        return "assess"
    return [Send("agent", {"assignment": item}) for item in assignments]


def _route_after_assess(state: _GraphState) -> str:
    return "plan" if state.get("failed") else "finalize"


# ------------------------------------------------------------------------ 工具


def _validate_assignments(planned: Any) -> tuple[list[Assignment], list[str]]:
    """把 planner 返回值收敛成合法 Assignment，非法项逐条记录错误。"""
    if planned is None:
        return [], []
    if isinstance(planned, (Assignment, dict)):
        planned = [planned]
    if not isinstance(planned, (list, tuple)):
        return [], [f"{PLANNER_INVALID}: planner 必须返回 Assignment 列表"]

    assignments: list[Assignment] = []
    errors: list[str] = []
    for raw in planned:
        try:
            assignment = _coerce_assignment(raw)
        except (ValidationError, TypeError) as exc:
            message = (
                _format_validation_error(exc)
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            errors.append(f"{INVALID_ASSIGNMENT}: 派发参数不合法：{message}")
            continue
        agent_type = assignment.agent_type.strip().lower()
        if agent_type not in DOMAIN_AGENT_TYPES:
            errors.append(
                f"{INVALID_ASSIGNMENT}: 不允许的 agent_type={agent_type!r}"
                "（仅允许 device/vision/diagnosis/operations/report）"
            )
            continue
        assignments.append(assignment.model_copy(update={"agent_type": agent_type}))
    return assignments, errors


def _coerce_assignment(raw: Any) -> Assignment:
    """支持 Assignment / dict；dict 兼容 ``agent_type|agent`` 与 ``goal|task``。"""
    if isinstance(raw, Assignment):
        return raw
    if not isinstance(raw, dict):
        raise TypeError("派发项必须是 Assignment 或 dict")
    agent_type = raw.get("agent_type", raw.get("agent", ""))
    goal = raw.get("goal", raw.get("task", ""))
    input_value = raw.get("input", {})
    if not isinstance(input_value, dict):
        raise TypeError("input 必须是 JSON 对象")
    return Assignment(agent_type=str(agent_type or ""), goal=str(goal or ""), input=input_value)


def _assignment_from_plan(item: Any) -> Assignment:
    """默认 planner 的宽松映射：LLM 可能用 ``agent`` / ``task`` 命名。"""
    try:
        return _coerce_assignment(item)
    except (ValidationError, TypeError) as exc:
        message = (
            _format_validation_error(exc)
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise SupervisorPlannerError(
            PLANNER_INVALID, f"计划项不合法：{message}"
        ) from exc


def _failed_assignment_dict(entry: dict) -> dict:
    """从结果条目还原重规划所需的 Assignment（保留原始 input）。"""
    return {
        "agent_type": entry.get("agent_type", ""),
        "goal": entry.get("goal", ""),
        "input": entry.get("input") if isinstance(entry.get("input"), dict) else {},
    }


def _entry_from_run(assignment: Assignment, run: Any) -> dict:
    """把 AgentRunResult 兼容对象转成结果条目；字段缺失不猜。"""
    status = _str_or_none(_field(run, "status")) or "failed"
    return {
        "agent_type": assignment.agent_type,
        "goal": assignment.goal,
        "input": assignment.input,
        "status": status,
        "output": _field(run, "output"),
        "error": _str_or_none(_field(run, "error")),
        "agent_run_id": _int_or_none(_field(run, "agent_run_id")),
        "total_tokens": _int_or_none(_field(run, "total_tokens")),
    }


def _failed_entry(*, agent_type: str, goal: str, input: dict, error: str) -> dict:
    return {
        "agent_type": agent_type,
        "goal": goal,
        "input": input,
        "status": "failed",
        "output": None,
        "error": error,
        "agent_run_id": None,
        "total_tokens": None,
    }


def _aggregate_output(
    results: list[dict], errors: list[str], dropped: int
) -> SupervisorOutput:
    """聚合子输出：assignments / evidence 原样保留，summary 只做计数。"""
    succeeded = sum(1 for entry in results if entry.get("status") == "succeeded")
    failed = len(results) - succeeded
    summary = (
        f"Supervisor 完成 {len(results)} 次 Agent 派发："
        f"成功 {succeeded}，失败 {failed}。"
    )
    if dropped:
        summary += f"另有 {dropped} 个派发因调用上限被丢弃。"
    if errors:
        summary += f"编排告警 {len(errors)} 条，详见 errors。"

    assignments: list[dict] = []
    evidence: list[Evidence] = []
    for entry in results:
        assignments.append(
            {
                "agent_type": entry.get("agent_type"),
                "goal": entry.get("goal"),
                "status": entry.get("status"),
                "output": entry.get("output"),
                "error": entry.get("error"),
            }
        )
        output = entry.get("output")
        raw_evidence = output.get("evidence") if isinstance(output, dict) else None
        if not isinstance(raw_evidence, list):
            continue
        for item in raw_evidence:
            try:
                evidence.append(Evidence.model_validate(item))
            except ValidationError:
                # 子 Agent 的证据不合法就不合并，绝不替它补造。
                continue

    return SupervisorOutput(
        plan=[str(entry.get("goal") or "") for entry in results],
        assignments=assignments,
        summary=summary,
        evidence=evidence,
    )


def _final_status(results: list[dict], dropped: int, errors: list[str]) -> str:
    """无结果为 ``failed``；有丢弃/告警/失败即 ``partial``；全成功才 ``succeeded``。"""
    if not results:
        return "failed"
    if dropped or errors:
        return "partial"
    if any(entry.get("status") != "succeeded" for entry in results):
        return "partial"
    return "succeeded"


def _sum_tokens(results: list[dict]) -> int:
    """只累加真实回报的整数 token；缺失按 0 计入（不估算）。"""
    total = 0
    for entry in results:
        tokens = entry.get("total_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool):
            total += tokens
    return total


def _primary_error(errors: list[str], results: list[dict]) -> str | None:
    """``error`` 优先取编排级错误，其次取第一条子任务错误。"""
    if errors:
        return errors[0]
    for entry in results:
        error = entry.get("error")
        if isinstance(error, str) and error:
            return error
    return None


def _planner_messages(
    task: dict, *, failed: list[Assignment] | None, replans: int
) -> list[dict]:
    """默认 planner 的 messages：Supervisor prompt + 任务/重规划上下文。"""
    system = load_default_prompt(AGENT_TYPE) or "你是 EdgePilot 的编排 Agent（Supervisor）。"
    payload = {
        "task": task,
        "replans": replans,
        "failed_assignments": [
            item.model_dump() for item in (failed or [])
        ],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]


def _candidate_from_response(response: Any) -> dict | None:
    content = response.content if isinstance(getattr(response, "content", None), str) else ""
    if content.strip():
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            return data
    structured = getattr(response, "structured", None)
    return structured if isinstance(structured, dict) else None


def _format_validation_error(exc: ValidationError) -> str:
    """压缩校验错误，最多保留前 3 条。"""
    parts: list[str] = []
    for item in exc.errors()[:3]:
        loc = ".".join(str(part) for part in item.get("loc", ()))
        parts.append(f"{loc}: {item.get('msg', 'invalid')}")
    return "; ".join(parts) or "未知校验错误"


def _field(value: Any, name: str) -> Any:
    """兼容 dataclass 对象与 dict 形式的 runner 返回值。"""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _id_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
