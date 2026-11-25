"""工作流执行引擎：按 DSL 图推进节点、落库状态并接入观测。

设计约定：

- 运行行按主键读取（worker/job 路径唯一允许的跨租户读）；版本、工作流与
  节点行随后一律按 ``run.tenant_id`` 过滤。
- 每次执行前重读 run 状态，取消后立刻停止，不覆盖取消服务写入的终态。
- 节点结果持久化后才推进到后继；已 ``succeeded`` / ``skipped`` 的节点不重跑，
  崩溃遗留的 ``running`` 节点通过 attempt+1 重试。
- 审批命中会释放 worker：run 与节点都停在 ``waiting_approval``，trace 不关闭，
  等审批服务批准后以 ``resume_node_id`` 再入队。
- 观测是旁路：任何 tracer / span 异常都被吞掉并降级为 Null 句柄，绝不影响
  节点执行与运行结果。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import run_agent
from app.agents.llm import LLMClient
from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.agent import Agent
from app.models.run import WorkflowNodeRun, WorkflowRun
from app.observability import context as obs_context
from app.observability import get_tracer
from app.observability.span import NullSpan, NullTrace, TraceHandle
from app.repositories.workflow_repo import WorkflowRepo
from app.tools.executor import ToolExecutionError
from app.tools.registry import ToolContext, get_default_registry
from app.workflow.dsl import edges_of, node_map
from app.workflow.expression import (
    ExpressionError,
    evaluate_condition,
    evaluate_expression,
)

logger = logging.getLogger(__name__)

RUN_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "timeout", "cancelled"}
)
NODE_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "skipped", "waiting_approval"}
)
DEFAULT_NODE_TIMEOUT_MS = 30000
DEFAULT_APPROVAL_TIMEOUT_MS = 86_400_000


@dataclass
class EngineResult:
    """一次引擎推进的返回值；供任务层转成小 dict 记录日志。"""

    run_id: int | None
    status: str
    output: Any = None
    error: str | None = None
    resume_node_id: str | None = None
    waiting_node_id: str | None = None


@dataclass
class _NodeResult:
    status: str
    output: Any = None
    error: str | None = None
    error_code: str | None = None
    retryable: bool = False


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """观测调用统一兜底：失败只记 debug，绝不改变业务结果。"""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 观测失败不能影响业务
        logger.debug("观测调用失败（已忽略）", exc_info=True)
        return None


def _json_safe(value: Any) -> tuple[bool, Any]:
    """把节点输出压成 JSON 原生类型；循环引用等无法序列化时返回失败。"""
    try:
        return True, json.loads(
            json.dumps(value, default=str, ensure_ascii=False)
        )
    except (TypeError, ValueError):
        return False, None


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class WorkflowEngine:
    """单次运行推进器。

    Args:
        session: 调用方会话；引擎自行提交每个节点的状态变化。
        run_id: 要推进的运行主键。也允许在 ``run()`` 时用同名关键字覆盖。
        tracer: 观测 tracer；默认进程级 ``get_tracer()``。
        agent_runner: Agent 执行函数；默认 ``app.agents.base.run_agent``。
        tool_executor: 工具执行函数；默认默认注册表的 ``execute``。
        sleep: 退避函数；默认 ``asyncio.sleep``（测试注入以避免真实等待）。
        now: 时钟；默认 UTC ``datetime.now``（测试注入固定时间）。
        stale_after_s: 节点租约过期秒数；默认取 ``workflow_stale_after_s``。
            心跳仍新鲜的 ``running`` 节点视为被其它 worker 持有，不再抢占。
        heartbeat_factory: 心跳会话工厂；提供时长时间节点会在独立会话里续租。
            默认 ``None``（不续租，只依赖节点执行期间的一次性心跳）。
        heartbeat_interval_s: 心跳续租间隔；默认 ``max(5, stale_after_s / 3)``。
    """

    def __init__(
        self,
        session: AsyncSession,
        run_id: int | None = None,
        *,
        tracer: Any | None = None,
        agent_runner: Callable[..., Any] | None = None,
        tool_executor: Callable[..., Any] | None = None,
        sleep: Callable[[float], Any] | None = None,
        now: Callable[[], datetime] | None = None,
        stale_after_s: int | None = None,
        heartbeat_factory: Callable[[], Any] | None = None,
        heartbeat_interval_s: float | None = None,
    ) -> None:
        self.session = session
        self.run_id = run_id
        self.tracer = tracer if tracer is not None else get_tracer()
        self.agent_runner = agent_runner or run_agent
        self.tool_executor = tool_executor or get_default_registry().execute
        self.sleep = sleep or asyncio.sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.stale_after_s = (
            stale_after_s
            if stale_after_s is not None
            else get_settings().workflow_stale_after_s
        )
        self.heartbeat_factory = heartbeat_factory
        self.heartbeat_interval_s = (
            heartbeat_interval_s
            if heartbeat_interval_s is not None
            else max(5.0, self.stale_after_s / 3)
        )
        self.repo = WorkflowRepo(session)

        self.workflow = None
        self.version = None
        self.dsl: dict[str, Any] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, WorkflowNodeRun] = {}
        self.outgoing: dict[str, list[dict[str, Any]]] = {}
        self.incoming: dict[str, list[dict[str, Any]]] = {}
        self.trace: Any | None = None

    # ------------------------------------------------------------------ 入口
    async def run(
        self,
        *,
        resume_node_id: str | None = None,
        run_id: int | None = None,
    ) -> EngineResult:
        """推进运行；终态幂等返回，审批暂停返回 ``waiting_approval``。"""
        resolved = run_id if run_id is not None else self.run_id
        if resolved is None:
            raise ValueError("WorkflowEngine 需要 run_id")
        self.run_id = resolved

        # worker/job 路径唯一允许的无租户读：先拿 run，再据其 tenant 收口其余查询。
        # 用加锁读串行化重复投递：两条 Celery 任务不能同时开始同一条 trace。
        run = await self.session.scalar(
            select(WorkflowRun)
            .where(WorkflowRun.id == resolved)
            .with_for_update()
        )
        if run is None:
            return EngineResult(
                run_id=resolved, status="not_found", error="run_not_found"
            )

        previous_trace = obs_context.current_trace.get()
        previous_span = obs_context.current_span.get()
        token = set_tenant_id(run.tenant_id)
        try:
            return await self._run_inner(run, resume_node_id)
        finally:
            reset_tenant_id(token)
            obs_context.current_trace.set(previous_trace)
            obs_context.current_span.set(previous_span)

    # ------------------------------------------------------------------ 内部
    async def _run_inner(
        self, run: WorkflowRun, resume_node_id: str | None
    ) -> EngineResult:
        if run.status in RUN_TERMINAL_STATUSES:
            return EngineResult(
                run_id=run.id, status=run.status, error=run.error
            )

        await self._load_graph(run)
        if self.version is None or self.workflow is None:
            # 图已无法加载时也要收口已开启的 trace，避免残留 running trace。
            # 重建句柄属于观测旁路：任何异常都只记日志，绝不阻断失败收敛。
            if run.trace_id:
                try:
                    handle = TraceHandle(
                        tracer=self.tracer,
                        tenant_id=run.tenant_id,
                        trace_id=run.trace_id,
                        root_span_id=run.root_span_id or "",
                        name=f"workflow_run.{run.id}",
                        source="workflow_run",
                        start_time=run.started_at or self._now(),
                    )
                    _safe(
                        handle.set_output,
                        {"error": "workflow_version_not_found"},
                    )
                    _safe(handle.close, "failed")
                except Exception:  # noqa: BLE001 - 观测失败不能影响失败收敛
                    logger.debug("关闭残留 trace 失败（run_id=%s）", run.id, exc_info=True)
            await self._finish_failure(run, "workflow_version_not_found")
            return EngineResult(
                run_id=run.id,
                status="failed",
                error="workflow_version_not_found",
            )

        if run.status == "waiting_approval":
            waiting = next(
                (
                    row
                    for row in self.rows.values()
                    if row.status == "waiting_approval"
                ),
                None,
            )
            return EngineResult(
                run_id=run.id,
                status="waiting_approval",
                waiting_node_id=waiting.node_id if waiting is not None else None,
            )

        if run.status not in ("pending", "running"):
            await self._finish_failure(run, f"invalid_run_status: {run.status}")
            return EngineResult(
                run_id=run.id, status="failed", error=run.error
            )

        # 第一次执行：pending → running，只写一次 started_at。
        if run.status == "pending":
            run.status = "running"
            if run.started_at is None:
                run.started_at = self._now()
            run.finished_at = None
            run.error = None

        self.trace = self._setup_trace(run)
        await self.session.commit()

        start_ids = (
            [resume_node_id]
            if isinstance(resume_node_id, str) and resume_node_id
            else self._start_node_ids()
        )
        if not start_ids:
            await self._finish_failure(run, "start_node_not_found")
            return EngineResult(
                run_id=run.id, status="failed", error=run.error
            )

        return await self._traverse(run, start_ids)

    async def _load_graph(self, run: WorkflowRun) -> None:
        """按 run.tenant_id 读取版本/工作流/节点行，并建立邻接表。"""
        # 先读节点行：即使版本缺失，收敛失败时也能把非终态节点标为 skipped。
        rows = await self.repo.list_node_runs(run.tenant_id, run.id)
        self.rows = {row.node_id: row for row in rows}

        version = await self.repo.get_version_by_id(
            run.tenant_id, run.workflow_version_id
        )
        if version is None:
            return
        workflow = await self.repo.get_workflow(run.tenant_id, version.workflow_id)
        if workflow is None:
            return

        self.version = version
        self.workflow = workflow
        self.dsl = version.dsl if isinstance(version.dsl, dict) else {}
        self.nodes = node_map(self.dsl)

        # 防御：历史数据缺行时补一条 pending，保证每个 DSL 节点都可幂等推进。
        missing = [node_id for node_id in self.nodes if node_id not in self.rows]
        for node_id in missing:
            node = self.nodes[node_id]
            row = WorkflowNodeRun(
                tenant_id=run.tenant_id,
                run_id=run.id,
                node_id=node_id,
                node_type=node.get("type", "unknown"),
                status="pending",
                attempt=1,
            )
            self.session.add(row)
            self.rows[node_id] = row
        if missing:
            await self.session.flush()

        self.outgoing = {node_id: [] for node_id in self.nodes}
        self.incoming = {node_id: [] for node_id in self.nodes}
        for edge in edges_of(self.dsl):
            source = edge.get("from")
            target = edge.get("to")
            if source in self.outgoing and target in self.nodes:
                self.outgoing[source].append(edge)
                self.incoming[target].append(edge)

    # ------------------------------------------------------------------ trace
    def _setup_trace(self, run: WorkflowRun) -> Any:
        """首跑开新 trace；中断恢复复用落库的 trace_id，绝不重复开 trace。"""
        name = f"workflow.{getattr(self.workflow, 'name', run.id)}"
        if run.trace_id:
            handle = TraceHandle(
                tracer=self.tracer,
                tenant_id=run.tenant_id,
                trace_id=run.trace_id,
                root_span_id=run.root_span_id or "",
                name=name,
                source="workflow_run",
                start_time=run.started_at or self._now(),
            )
            obs_context.current_trace.set(handle)
            obs_context.current_span.set(None)
            return handle

        try:
            handle = self.tracer.start_trace(
                name, source="workflow_run", input=run.input
            )
        except Exception:  # noqa: BLE001 - 观测失败不阻断执行
            logger.warning("工作流 trace 开启失败，降级为无观测执行", exc_info=True)
            handle = NullTrace("trace_start_failed")
            _safe(obs_context.current_trace.set, handle)
            _safe(obs_context.current_span.set, None)

        trace_id = getattr(handle, "trace_id", "") or ""
        root_span_id = getattr(handle, "root_span_id", "") or ""
        if trace_id:
            run.trace_id = trace_id
        if root_span_id:
            run.root_span_id = root_span_id
        return handle

    def _start_span(
        self,
        node: dict[str, Any],
        attempt: int,
        *,
        tool_id: str | None = None,
    ) -> Any:
        node_id = str(node.get("id") or "")
        span_type = str(node.get("type") or "node")
        try:
            return self.tracer.start_span(
                f"node.{node_id}",
                span_type,
                attempt=attempt,
                node_id=node_id,
                tool_id=tool_id,
            )
        except Exception:  # noqa: BLE001 - 观测失败不能阻断节点
            logger.warning("节点 span 开启失败（node_id=%s）", node_id, exc_info=True)
            return NullSpan()

    def _close_trace(self, status: str, output: Any) -> None:
        if self.trace is None:
            return
        _safe(self.trace.set_output, output)
        _safe(self.trace.close, status)

    # ------------------------------------------------------------------ 遍历
    async def _traverse(
        self, run: WorkflowRun, start_ids: list[str]
    ) -> EngineResult:
        queue: deque[str] = deque(start_ids)
        processed: set[str] = set()
        reached_end = False
        end_output: Any = None

        while queue:
            node_id = queue.popleft()
            if node_id in processed:
                continue
            processed.add(node_id)

            await self._refresh_run(run)
            if run.status == "cancelled":
                return EngineResult(run_id=run.id, status="cancelled")
            if run.status in RUN_TERMINAL_STATUSES:
                return EngineResult(
                    run_id=run.id, status=run.status, error=run.error
                )

            node = self.nodes.get(node_id)
            row = self.rows.get(node_id)
            if node is None or row is None:
                continue

            if row.status in ("succeeded", "skipped"):
                if node.get("type") == "end":
                    reached_end = True
                    if end_output is None:
                        end_output = row.output
                    continue
                queue.extend(self._next_node_ids(node, row))
                continue

            result = await self._execute_node(run, node, row)
            if result.status == "busy":
                # 另一 worker 仍持有该节点租约：立刻让出，不失败 run、不推进后继。
                return EngineResult(run_id=run.id, status="busy")
            if result.status == "cancelled":
                return EngineResult(run_id=run.id, status="cancelled")
            if result.status == "waiting_approval":
                return EngineResult(
                    run_id=run.id,
                    status="waiting_approval",
                    waiting_node_id=node_id,
                )
            if result.status == "failed":
                await self._finish_failure(run, result.error or "node_failed")
                return EngineResult(
                    run_id=run.id, status="failed", error=result.error
                )

            if node.get("type") == "end":
                reached_end = True
                if end_output is None:
                    end_output = result.output
                continue
            queue.extend(self._next_node_ids(node, row))

        if not reached_end:
            error = "end_not_reached"
            await self._finish_failure(run, error)
            return EngineResult(run_id=run.id, status="failed", error=error)

        return await self._finish_success(run, end_output if end_output is not None else {})

    def _start_node_ids(self) -> list[str]:
        return [
            node_id
            for node_id, node in self.nodes.items()
            if node.get("type") == "start"
        ]

    def _next_node_ids(
        self, node: dict[str, Any], row: WorkflowNodeRun
    ) -> list[str]:
        outgoing = self.outgoing.get(str(node.get("id")), [])
        if node.get("type") == "condition":
            output = row.output if isinstance(row.output, dict) else {}
            chosen = bool(output.get("result"))
            wanted = "true" if chosen else "false"
            return [
                edge["to"]
                for edge in outgoing
                if str(edge.get("when", "")).strip().lower() == wanted
                and isinstance(edge.get("to"), str)
            ]
        return [
            edge["to"] for edge in outgoing if isinstance(edge.get("to"), str)
        ]

    # ------------------------------------------------------------- 节点分派
    async def _execute_node(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        node_type = node.get("type")
        if node_type == "start":
            return await self._execute_start(run, node, row)
        if node_type == "agent":
            return await self._execute_agent(run, node, row)
        if node_type == "tool":
            return await self._execute_tool(run, node, row)
        if node_type == "condition":
            return await self._execute_condition(run, node, row)
        if node_type == "approval":
            return await self._execute_approval(run, node, row)
        if node_type == "end":
            return await self._execute_end(run, node, row)
        return await self._fail_node(
            row, "unknown_node_type", f"未知节点类型: {node_type}"
        )

    # ------------------------------------------------------------- start/end
    async def _execute_start(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        if not await self._mark_running(row):
            return _NodeResult("busy")
        span = self._start_span(node, row.attempt)
        _safe(self._remember_span, row, span)
        _safe(span.set_input, {"input": run.input})
        output = run.input if isinstance(run.input, dict) else {}
        ok, safe_output = _json_safe(output)
        if not ok:
            _safe(span.set_error, "output_not_json", "输出无法序列化为 JSON")
            _safe(span.finish, "failed")
            return await self._fail_node(
                row, "output_not_json", "输出无法序列化为 JSON"
            )
        _safe(span.set_output, safe_output)
        _safe(span.finish, "succeeded")
        return await self._succeed_node(row, safe_output)

    async def _execute_end(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        if not await self._mark_running(row):
            return _NodeResult("busy")
        span = self._start_span(node, row.attempt)
        _safe(self._remember_span, row, span)
        predecessors = self.incoming.get(str(node.get("id")), [])
        output: Any = {}
        if len(predecessors) == 1:
            source = predecessors[0].get("from")
            if isinstance(source, str):
                source_row = self.rows.get(source)
                if source_row is not None and source_row.output is not None:
                    output = source_row.output
        ok, safe_output = _json_safe(output)
        if not ok:
            _safe(span.set_error, "output_not_json", "输出无法序列化为 JSON")
            _safe(span.finish, "failed")
            return await self._fail_node(
                row, "output_not_json", "输出无法序列化为 JSON"
            )
        _safe(span.set_output, safe_output)
        _safe(span.finish, "succeeded")
        return await self._succeed_node(row, safe_output)

    # -------------------------------------------------------------- condition
    async def _execute_condition(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        if not await self._mark_running(row):
            return _NodeResult("busy")
        span = self._start_span(node, row.attempt)
        _safe(self._remember_span, row, span)
        source = node.get("expression")
        _safe(span.set_input, {"expression": source})
        try:
            result = evaluate_condition(
                str(source), self._build_context(run)
            )
        except ExpressionError as exc:
            _safe(span.set_error, "expression_error", exc.message)
            _safe(span.finish, "failed")
            return await self._fail_node(row, "expression_error", exc.message)
        except Exception as exc:  # noqa: BLE001 - 表达式异常必须落成节点失败
            message = f"{type(exc).__name__}: {exc}"
            _safe(span.set_error, "expression_error", message)
            _safe(span.finish, "failed")
            return await self._fail_node(row, "expression_error", message)

        output = {"result": bool(result)}
        if await self._cancelled_after_execution(run):
            return _NodeResult("cancelled")
        _safe(span.set_output, output)
        _safe(span.finish, "succeeded")
        return await self._succeed_node(row, output)

    # ----------------------------------------------------------------- agent
    async def _execute_agent(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        agent_name = node.get("agent")
        if not isinstance(agent_name, str) or not agent_name.strip():
            return await self._fail_node(
                row, "agent_not_found_or_disabled", "agent 节点缺少 agent 名称"
            )
        agent = await self.session.scalar(
            select(Agent).where(
                Agent.tenant_id == run.tenant_id,
                Agent.name == agent_name,
                Agent.status == "active",
            )
        )
        if agent is None:
            return await self._fail_node(
                row,
                "agent_not_found_or_disabled",
                f"Agent 不存在或已禁用: {agent_name}",
            )

        try:
            task = self._resolve_input_map(node, run)
        except ExpressionError as exc:
            return await self._fail_node(row, "expression_error", exc.message)
        except Exception as exc:  # noqa: BLE001
            return await self._fail_node(
                row, "expression_error", f"{type(exc).__name__}: {exc}"
            )

        settings = get_settings()
        config = agent.config if isinstance(agent.config, dict) else {}
        fixture_name = config.get("mock_fixture")
        llm = LLMClient(
            mode=settings.llm_mode,
            fixture_dir=settings.llm_fixture_dir,
            fixture_name=fixture_name if isinstance(fixture_name, str) else None,
        )

        async def attempt(span: Any) -> _NodeResult:
            _safe(span.set_input, task)
            result = await self.agent_runner(
                agent,
                task,
                tracer=self.tracer,
                llm=llm,
                session=self.session,
                tenant_id=run.tenant_id,
                trace_handle=self.trace,
                span_handle=span,
            )
            status = getattr(result, "status", "failed")
            if status != "succeeded":
                error = getattr(result, "error", None) or "agent_failed"
                code = str(error).split(":", 1)[0] if ":" in str(error) else "agent_failed"
                return _NodeResult(
                    "failed", error=str(error), error_code=code, retryable=True
                )
            ok, output = _json_safe(getattr(result, "output", None))
            if not ok:
                return _NodeResult(
                    "failed",
                    error="output_not_json: 输出无法序列化为 JSON",
                    error_code="output_not_json",
                )
            _safe(span.set_output, output)
            return _NodeResult("succeeded", output=output)

        return await self._run_with_retries(run, node, row, attempt)

    # ------------------------------------------------------------------ tool
    async def _execute_tool(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        tool_id = node.get("tool")
        if not isinstance(tool_id, str) or not tool_id.strip():
            return await self._fail_node(
                row, "tool_not_found_or_disabled", "tool 节点缺少 tool 标识"
            )
        try:
            args = self._resolve_input_map(node, run)
        except ExpressionError as exc:
            return await self._fail_node(row, "expression_error", exc.message)
        except Exception as exc:  # noqa: BLE001
            return await self._fail_node(
                row, "expression_error", f"{type(exc).__name__}: {exc}"
            )

        ctx = ToolContext(
            tenant_id=run.tenant_id,
            session=self.session,
            trace_id=run.trace_id,
            idempotency_key=f"{run.id}:{row.node_id}",
        )

        async def attempt(span: Any) -> _NodeResult:
            _safe(span.set_input, args)
            try:
                result = await self.tool_executor(tool_id, args, ctx)
            except ToolExecutionError as exc:
                return _NodeResult(
                    "failed",
                    error=f"{exc.code}: {exc.message}",
                    error_code=exc.code,
                    retryable=exc.retryable,
                )
            if not getattr(result, "ok", True):
                code = getattr(result, "error_code", None) or "execution_failed"
                return _NodeResult(
                    "failed", error=code, error_code=code, retryable=True
                )
            ok, output = _json_safe(getattr(result, "data", None))
            if not ok:
                return _NodeResult(
                    "failed",
                    error="output_not_json: 输出无法序列化为 JSON",
                    error_code="output_not_json",
                )
            _safe(span.set_output, output)
            return _NodeResult("succeeded", output=output)

        return await self._run_with_retries(
            run, node, row, attempt, tool_id=tool_id
        )

    # -------------------------------------------------------------- approval
    async def _execute_approval(
        self, run: WorkflowRun, node: dict[str, Any], row: WorkflowNodeRun
    ) -> _NodeResult:
        # 先抢占租约再写 waiting_approval，避免重复投递把审批状态写两遍。
        if not await self._mark_running(row):
            return _NodeResult("busy")
        now = self._now()
        timeout_ms = _positive_int(
            node.get("timeout_ms"), DEFAULT_APPROVAL_TIMEOUT_MS
        )
        expires_at = now + timedelta(milliseconds=timeout_ms)
        title = node.get("title") if isinstance(node.get("title"), str) else str(
            node.get("id")
        )
        approvers = node.get("approvers") if isinstance(node.get("approvers"), list) else []

        row.status = "waiting_approval"
        if row.started_at is None:
            row.started_at = now
        row.heartbeat_at = now
        row.error = None
        row.output = None
        row.approval = {
            "title": title,
            "approvers": list(approvers),
            "created_at": _iso(now),
            "expires_at": _iso(expires_at),
            "decision": None,
            "by": None,
            "at": None,
            "comment": None,
        }
        run.status = "waiting_approval"
        run.finished_at = None
        await self.session.commit()

        span = self._start_span(node, row.attempt)
        _safe(self._remember_span, row, span)
        _safe(span.set_input, {"title": title, "approvers": list(approvers)})
        _safe(
            span.set_meta,
            **{
                "title": title,
                "approvers": list(approvers),
                "expires_at": _iso(expires_at),
            },
        )
        _safe(
            span.add_event,
            "approval_requested",
            {"approvers": list(approvers), "expires_at": _iso(expires_at)},
        )
        _safe(span.finish, "waiting_approval")
        return _NodeResult("waiting_approval")

    # ---------------------------------------------------------- 重试与收口
    async def _run_with_retries(
        self,
        run: WorkflowRun,
        node: dict[str, Any],
        row: WorkflowNodeRun,
        attempt_fn: Callable[[Any], Any],
        *,
        tool_id: str | None = None,
    ) -> _NodeResult:
        max_retries = self._max_retries(node)
        total_attempts = max_retries + 1
        timeout_ms = _positive_int(node.get("timeout_ms"), DEFAULT_NODE_TIMEOUT_MS)
        timeout_s = timeout_ms / 1000

        # 原子抢占租约：失败说明另一 worker 心跳仍新鲜，直接让出。
        if not await self._mark_running(row):
            return _NodeResult("busy")
        last: _NodeResult | None = None

        for index in range(total_attempts):
            await self._refresh_run(run)
            if run.status == "cancelled":
                return _NodeResult("cancelled")
            if run.status in RUN_TERMINAL_STATUSES:
                return _NodeResult(
                    "failed", error=run.error or "run_terminal", error_code=run.status
                )
            if index > 0:
                row.status = "running"
                row.heartbeat_at = self._now()
                await self.session.commit()

            span = self._start_span(node, row.attempt, tool_id=tool_id)
            _safe(self._remember_span, row, span)
            # 长节点超过 stale 阈值时由独立会话续租，避免被 stale 扫描重新投递。
            heartbeat = self._start_heartbeat(row.id)
            try:
                outcome: _NodeResult = await asyncio.wait_for(
                    attempt_fn(span), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                message = f"节点执行超过 {timeout_ms}ms"
                outcome = _NodeResult(
                    "failed",
                    error=f"timeout: {message}",
                    error_code="timeout",
                    retryable=True,
                )
            except ExpressionError as exc:
                outcome = _NodeResult(
                    "failed",
                    error=f"expression_error: {exc.message}",
                    error_code="expression_error",
                )
            except Exception as exc:  # noqa: BLE001 - 节点异常必须落库为失败
                logger.exception(
                    "节点执行异常（run_id=%s, node_id=%s）", run.id, row.node_id
                )
                message = f"{type(exc).__name__}: {exc}"
                outcome = _NodeResult(
                    "failed",
                    error=f"internal_error: {message}",
                    error_code="internal_error",
                    retryable=True,
                )
            finally:
                await self._stop_heartbeat(heartbeat)

            if outcome.status == "succeeded":
                if await self._cancelled_after_execution(run):
                    return _NodeResult("cancelled")
                _safe(span.finish, "succeeded")
                return await self._succeed_node(row, outcome.output)

            last = outcome
            _safe(
                span.set_error,
                outcome.error_code or "node_failed",
                outcome.error or "",
                retryable=outcome.retryable,
            )
            _safe(span.finish, "failed")

            row.heartbeat_at = self._now()
            await self.session.commit()

            # 不可重试的失败立即收敛：不消耗剩余重试次数，也不累加 attempt。
            if not outcome.retryable:
                break

            if index < total_attempts - 1:
                row.attempt = max(1, row.attempt) + 1
                row.heartbeat_at = self._now()
                await self.session.commit()
                await self._wait_backoff(index)

        assert last is not None
        return await self._fail_node(
            row,
            last.error_code or "node_failed",
            last.error or "",
            retryable=last.retryable,
        )

    async def _wait_backoff(self, index: int) -> None:
        delay = min(16, 4**index)
        try:
            result = self.sleep(delay)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - 退避失败不改变节点结果
            logger.warning("节点退避等待失败", exc_info=True)

    def _max_retries(self, node: dict[str, Any]) -> int:
        explicit = _non_negative_int(node.get("max_retries"))
        if explicit is not None:
            return explicit
        return 2 if node.get("type") in ("agent", "tool") else 0

    # ------------------------------------------------------------ 状态写入
    def _start_heartbeat(self, row_id: int) -> asyncio.Task | None:
        """为长节点启动心跳守卫；没有会话工厂时返回 ``None``（不续租）。"""
        if self.heartbeat_factory is None:
            return None
        return asyncio.create_task(self._heartbeat_loop(row_id))

    async def _stop_heartbeat(self, task: asyncio.Task | None) -> None:
        """取消并等待心跳任务退出；取消是正常路径，不向上抛出。"""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self, row_id: int) -> None:
        """周期用独立会话刷新节点心跳；任何非取消失败都只记 debug。"""
        while True:
            # 必须用真实 asyncio.sleep，不能复用可注入的退避 sleep。
            await asyncio.sleep(self.heartbeat_interval_s)
            try:
                async with self.heartbeat_factory() as hb_session:
                    await hb_session.execute(
                        update(WorkflowNodeRun)
                        .where(WorkflowNodeRun.id == row_id)
                        .values(heartbeat_at=self._now())
                    )
                    await hb_session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 心跳失败不能影响节点执行
                logger.debug(
                    "节点心跳刷新失败（node_run_id=%s）", row_id, exc_info=True
                )

    async def _mark_running(self, row: WorkflowNodeRun) -> bool:
        """原子抢占节点租约，返回是否成功。

        用一条条件 UPDATE 完成「抢占 + 心跳 + 起算 attempt」：只有节点仍
        ``pending`` / ``running`` / ``failed`` 且心跳为空或已过期时才更新成功。
        心跳仍新鲜意味着另一个 worker 正持有该节点（租约未过期），本次返回
        ``False``，调用方必须以 ``busy`` 让出，绝不能重复调用 LLM 或工具。
        """
        now = self._now()
        stale_before = now - timedelta(seconds=self.stale_after_s)
        statement = (
            update(WorkflowNodeRun)
            .where(
                WorkflowNodeRun.id == row.id,
                WorkflowNodeRun.status.in_(("pending", "running", "failed")),
                or_(
                    WorkflowNodeRun.heartbeat_at.is_(None),
                    WorkflowNodeRun.heartbeat_at < stale_before,
                ),
            )
            .values(
                status="running",
                heartbeat_at=now,
                started_at=func.coalesce(WorkflowNodeRun.started_at, now),
                attempt=case(
                    (
                        WorkflowNodeRun.status.in_(("running", "failed")),
                        WorkflowNodeRun.attempt + 1,
                    ),
                    else_=func.greatest(WorkflowNodeRun.attempt, 1),
                ),
            )
            .execution_options(synchronize_session=False)
        )
        result = await self.session.execute(statement)
        await self.session.commit()
        await self.session.refresh(row)
        return result.rowcount == 1

    def _remember_span(self, row: WorkflowNodeRun, span: Any) -> None:
        span_id = getattr(span, "span_id", "") or ""
        if span_id:
            row.span_id = span_id

    async def _succeed_node(self, row: WorkflowNodeRun, output: Any) -> _NodeResult:
        now = self._now()
        row.status = "succeeded"
        row.output = output
        row.error = None
        row.finished_at = now
        row.heartbeat_at = now
        await self.session.commit()
        return _NodeResult("succeeded", output=output)

    async def _fail_node(
        self,
        row: WorkflowNodeRun,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> _NodeResult:
        now = self._now()
        error = message if message.startswith(code) else f"{code}: {message}"
        row.status = "failed"
        row.error = error
        row.finished_at = now
        row.heartbeat_at = now
        await self.session.commit()
        return _NodeResult(
            "failed",
            error=error,
            error_code=code,
            retryable=retryable,
        )

    async def _cancelled_after_execution(self, run: WorkflowRun) -> bool:
        """节点执行可能较久；落库成功前再读一次取消状态。"""
        await self._refresh_run(run)
        return run.status == "cancelled"

    async def _refresh_run(self, run: WorkflowRun) -> None:
        try:
            await self.session.refresh(run)
        except Exception:  # noqa: BLE001 - 刷新失败沿用当前对象
            logger.debug("刷新运行状态失败", exc_info=True)

    async def _skip_non_terminal(
        self, run: WorkflowRun, *, exclude: str | None = None
    ) -> None:
        now = self._now()
        for node_id, row in self.rows.items():
            if node_id == exclude:
                continue
            if row.status in ("pending", "running", "waiting_approval"):
                row.status = "skipped"
                row.finished_at = now
            if row.heartbeat_at is None:
                row.heartbeat_at = now

    async def _finish_success(self, run: WorkflowRun, output: Any) -> EngineResult:
        ok, safe_output = _json_safe(output)
        if not ok:
            await self._finish_failure(run, "output_not_json: 输出无法序列化为 JSON")
            return EngineResult(
                run_id=run.id,
                status="failed",
                error="output_not_json: 输出无法序列化为 JSON",
            )
        run.status = "succeeded"
        run.output = safe_output
        run.error = None
        run.finished_at = self._now()
        await self._skip_non_terminal(run)
        await self.session.commit()
        self._close_trace("succeeded", safe_output)
        return EngineResult(run_id=run.id, status="succeeded", output=safe_output)

    async def _finish_failure(self, run: WorkflowRun, error: str) -> None:
        run.status = "failed"
        run.error = error
        run.finished_at = self._now()
        await self._skip_non_terminal(run)
        await self.session.commit()
        self._close_trace("failed", {"status": "failed", "error": error})

    # ------------------------------------------------------------ 输入解析
    def _resolve_input_map(
        self, node: dict[str, Any], run: WorkflowRun
    ) -> dict[str, Any]:
        raw = node.get("input_map")
        if not isinstance(raw, dict):
            return dict(run.input) if isinstance(run.input, dict) else {}
        context = self._build_context(run)
        resolved: dict[str, Any] = {}
        for key, value in raw.items():
            if isinstance(value, str) and value.startswith("$"):
                resolved[key] = evaluate_expression(value, context)
            else:
                resolved[key] = value
        return resolved

    def _build_context(self, run: WorkflowRun) -> dict[str, Any]:
        """只暴露成功节点输出；形状固定 ``{"input": ..., "<node>": {"output": ...}}``。"""
        context: dict[str, Any] = {
            "input": run.input if isinstance(run.input, dict) else {}
        }
        for node_id, row in self.rows.items():
            if row.status == "succeeded":
                context[node_id] = {"output": row.output}
        return context

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


__all__ = [
    "DEFAULT_APPROVAL_TIMEOUT_MS",
    "DEFAULT_NODE_TIMEOUT_MS",
    "EngineResult",
    "WorkflowEngine",
]
