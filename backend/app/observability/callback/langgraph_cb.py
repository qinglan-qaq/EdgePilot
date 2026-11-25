"""LangGraph / LangChain 回调：把图执行映射到观测 span 树。

映射规则（设计 §4）：

* ``on_chain_start`` 且 ``parent_run_id is None`` → workflow 根 span。
  已有活动 trace 时**复用**它的 ``root_span_id``；否则由本回调
  ``tracer.start_trace(..., source="langgraph")`` 开 trace。``start_trace``
  已经写入根 workflow span，这里绝不再开第二个。
* 带 ``metadata["langgraph_node"]`` 的链 → ``node.<name>`` span，父 span 固定
  为 workflow 根。LangGraph 会把该 metadata 传给节点内部嵌套的 Runnable，
  因此父链上已有同名 node span 的链视为同一节点的内部链，不再重复建 span。
* ``on_llm_*`` / ``on_chat_model_start`` / ``on_tool_*`` → 挂在最近的活动节点
  （或 workflow 根）下。若当前上下文里已经有一个同类型业务 span（业务侧埋点
  包住了这次调用），直接复用，不重复创建。
* ``on_interrupt`` / ``on_resume`` 只写事件，不引入第二状态机。

所有公开回调都做异常隔离：观测层任何失败只记日志，绝不把异常抛回图执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from app.observability import context as obs_context
from app.observability.span import NullSpan, NullTrace, SpanHandle

logger = logging.getLogger(__name__)

_KIND_NODE = "node"
_KIND_LLM = "llm"
_KIND_TOOL = "tool"
_KIND_ROOT = "root"
_KIND_IGNORED = "ignored"


@dataclass
class _RunState:
    """一次 callback run 的观测状态。"""

    kind: str
    span: Any | None
    parent: str | None
    node_name: str | None = None
    reused: bool = False


def _key(run_id: Any) -> str:
    return str(run_id) if run_id is not None else ""


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _graph_name(serialized: Any) -> str:
    """从 callback 的 serialized 里取图名；LangGraph 根链常为 None。"""
    name = None
    if isinstance(serialized, dict):
        name = _non_empty_str(serialized.get("name"))
        if name is None:
            identifier = serialized.get("id")
            if isinstance(identifier, (list, tuple)) and identifier:
                name = _non_empty_str(identifier[-1])
    name = name or "langgraph"
    return name if name.startswith("workflow.") else f"workflow.{name}"


def _model_name(serialized: Any, metadata: dict) -> str:
    name = _non_empty_str(metadata.get("ls_model_name"))
    kwargs = serialized.get("kwargs") if isinstance(serialized, dict) else None
    if name is None and isinstance(kwargs, dict):
        name = _non_empty_str(kwargs.get("model_name")) or _non_empty_str(
            kwargs.get("model")
        )
    if name is None and isinstance(serialized, dict):
        name = _non_empty_str(serialized.get("name"))
    return name or "unknown"


def _tool_name(serialized: Any, metadata: dict) -> str:
    name = _non_empty_str(metadata.get("tool_name"))
    if name is None and isinstance(serialized, dict):
        name = _non_empty_str(serialized.get("name"))
        if name is None:
            identifier = serialized.get("id")
            if isinstance(identifier, (list, tuple)) and identifier:
                name = _non_empty_str(identifier[-1])
    return name or "tool"


def _llm_output(response: Any) -> dict | None:
    """只提取 callback 真实给出的 usage/批次数，不猜响应内容。"""
    if response is None:
        return None
    out: dict[str, Any] = {}
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, dict):
            out["token_usage"] = usage
    generations = getattr(response, "generations", None)
    if generations is not None:
        try:
            out["generation_batches"] = len(generations)
        except TypeError:
            pass
    return out or None


class LangGraphCallback(BaseCallbackHandler):
    """把 LangGraph 的 callback 事件转成 EdgePilot 观测记录。"""

    def __init__(self, tracer: Any | None = None) -> None:
        super().__init__()
        if tracer is None:
            try:
                from app.observability import get_tracer

                tracer = get_tracer()
            except Exception:  # noqa: BLE001 - 观测入口初始化失败不能影响图
                logger.warning("LangGraph 回调无法获取 tracer，观测关闭", exc_info=True)
                tracer = None
        self._tracer = tracer
        self._runs: dict[str, _RunState] = {}
        self._workflow_span_id: str | None = None
        self._trace_handle: Any | None = None
        self._owns_trace = False
        self._root_shell: SpanHandle | None = None
        self._root_events_pending = False

    # ---------------------------------------------------------------- chain

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._chain_start(serialized, inputs, _key(run_id),
                              _key(parent_run_id) or None, metadata or {})
        except Exception:  # noqa: BLE001 - 回调异常绝不影响图
            logger.warning("LangGraph on_chain_start 观测失败", exc_info=True)

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish(_key(run_id), outputs=outputs)
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_chain_end 观测失败", exc_info=True)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish(_key(run_id), error=error)
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_chain_error 观测失败", exc_info=True)

    # ------------------------------------------------------------------ llm

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._start_llm(
                _key(run_id), _key(parent_run_id) or None, serialized,
                metadata or {}, prompt_count=len(prompts or []),
            )
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_llm_start 观测失败", exc_info=True)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list[list[Any]],
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            message_count = sum(len(batch or []) for batch in (messages or []))
            self._start_llm(
                _key(run_id), _key(parent_run_id) or None, serialized,
                metadata or {}, prompt_count=message_count,
            )
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_chat_model_start 观测失败", exc_info=True)

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish(_key(run_id), outputs=_llm_output(response))
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_llm_end 观测失败", exc_info=True)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish(_key(run_id), error=error, error_code="llm_error")
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_llm_error 观测失败", exc_info=True)

    # ----------------------------------------------------------------- tool

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._start_tool(
                _key(run_id), _key(parent_run_id) or None, serialized,
                metadata or {}, input_str, inputs,
            )
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_tool_start 观测失败", exc_info=True)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish(_key(run_id), outputs=output)
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_tool_end 观测失败", exc_info=True)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        parent_run_id: Any = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish(_key(run_id), error=error, error_code="tool_error")
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_tool_error 观测失败", exc_info=True)

    # -------------------------------------------------------- interrupt/resume

    def on_interrupt(self, payload: dict | None = None) -> None:
        """记录一次 interrupt 事件（平台不启用 LangGraph HITL，仅埋点）。"""
        try:
            self._add_event("interrupt", payload)
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_interrupt 观测失败", exc_info=True)

    def on_resume(self, payload: dict | None = None) -> None:
        """记录一次 resume 事件。"""
        try:
            self._add_event("resume", payload)
        except Exception:  # noqa: BLE001
            logger.warning("LangGraph on_resume 观测失败", exc_info=True)

    # --------------------------------------------------------------- internals

    def _chain_start(
        self,
        serialized: Any,
        inputs: Any,
        run_id: str,
        parent_run_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        if not run_id or run_id in self._runs:
            return
        node_name = _non_empty_str(metadata.get("langgraph_node"))
        if parent_run_id is None:
            self._start_root(run_id, serialized, inputs)
            return
        if node_name is None:
            # 非根、无 langgraph_node 的嵌套链：不建 span，仅保留父链信息。
            self._runs[run_id] = _RunState(_KIND_IGNORED, None, parent_run_id)
            return
        if self._has_node_ancestor(parent_run_id, node_name):
            # LangGraph 把 langgraph_node 传给节点内部 Runnable；同一节点的
            # 内部链不再建第二个 node span。
            self._runs[run_id] = _RunState(
                _KIND_IGNORED, None, parent_run_id, node_name
            )
            return
        self._start_node(run_id, node_name, inputs, parent_run_id)

    def _start_root(self, run_id: str, serialized: Any, inputs: Any) -> None:
        name = _graph_name(serialized)
        trace = obs_context.current_trace.get()
        self._owns_trace = False
        if trace is None:
            try:
                trace = self._tracer.start_trace(
                    name, source="langgraph", input=inputs
                )
                self._owns_trace = True
            except Exception:  # noqa: BLE001 - 观测失败降级
                logger.warning("LangGraph trace 开启失败，观测关闭", exc_info=True)
                trace = None

        self._trace_handle = trace
        self._workflow_span_id = (
            getattr(trace, "root_span_id", None) if trace is not None else None
        ) or None
        shell = self._ensure_root_shell() if self._workflow_span_id else None
        self._runs[run_id] = _RunState(_KIND_ROOT, shell, None)
        if shell is not None:
            # 仅为 interrupt/resume 兜底保存输入；无事件时根 span 不重写。
            try:
                shell.set_input(inputs)
            except Exception:  # noqa: BLE001
                logger.debug("根 workflow span 记录输入失败", exc_info=True)

    def _start_node(
        self, run_id: str, node_name: str, inputs: Any, parent_run_id: str
    ) -> None:
        try:
            span = self._tracer.start_span(
                f"node.{node_name}", "workflow", node_id=node_name
            )
        except Exception:  # noqa: BLE001 - 单个 span 失败不影响图
            logger.debug("LangGraph 节点 span 开启失败：%s", node_name, exc_info=True)
            span = NullSpan()
        # Send/并行分支各自复制上下文，父 span 可能不在 current 里；
        # 显式把 node span 挂到 workflow 根（设计约定）。
        if self._workflow_span_id and hasattr(span, "parent_span_id"):
            span.parent_span_id = self._workflow_span_id
        try:
            span.set_meta(instrumented_by="langgraph", langgraph_node=node_name)
            span.set_input(inputs)
        except Exception:  # noqa: BLE001
            logger.debug("LangGraph 节点 span 记录输入失败", exc_info=True)
        self._runs[run_id] = _RunState(
            _KIND_NODE, span, parent_run_id, node_name
        )

    def _start_llm(
        self,
        run_id: str,
        parent_run_id: str | None,
        serialized: Any,
        metadata: dict[str, Any],
        *,
        prompt_count: int,
    ) -> None:
        if not run_id or run_id in self._runs:
            return
        model = _model_name(serialized, metadata)
        reuse = self._current_kind_span(_KIND_LLM)
        if reuse is not None:
            self._runs[run_id] = _RunState(
                _KIND_LLM, reuse, parent_run_id, reused=True
            )
            return
        span = self._open_child(
            f"llm.{model}", _KIND_LLM, parent_run_id, tool_id=None
        )
        try:
            span.set_input({"model_name": model, "prompt_count": prompt_count})
        except Exception:  # noqa: BLE001
            logger.debug("LangGraph llm span 记录输入失败", exc_info=True)
        self._runs[run_id] = _RunState(_KIND_LLM, span, parent_run_id)

    def _start_tool(
        self,
        run_id: str,
        parent_run_id: str | None,
        serialized: Any,
        metadata: dict[str, Any],
        input_str: Any,
        inputs: dict[str, Any] | None,
    ) -> None:
        if not run_id or run_id in self._runs:
            return
        name = _tool_name(serialized, metadata)
        reuse = self._current_kind_span(_KIND_TOOL)
        if reuse is not None:
            self._runs[run_id] = _RunState(
                _KIND_TOOL, reuse, parent_run_id, reused=True
            )
            return
        span = self._open_child(
            f"tool.{name}", _KIND_TOOL, parent_run_id, tool_id=name
        )
        try:
            span.set_input({
                "tool_name": name,
                "input": inputs if inputs is not None else input_str,
            })
        except Exception:  # noqa: BLE001
            logger.debug("LangGraph tool span 记录输入失败", exc_info=True)
        self._runs[run_id] = _RunState(_KIND_TOOL, span, parent_run_id)

    def _open_child(
        self, name: str, kind: str, parent_run_id: str | None, *,
        tool_id: str | None,
    ) -> Any:
        try:
            span = self._tracer.start_span(name, kind, tool_id=tool_id)
        except Exception:  # noqa: BLE001 - 观测失败不影响图
            logger.debug("LangGraph %s span 开启失败：%s", kind, name, exc_info=True)
            return NullSpan()
        ancestor = self._nearest_ancestor_span(parent_run_id)
        parent_span_id = (
            getattr(ancestor, "span_id", None) if ancestor is not None else None
        ) or self._workflow_span_id
        if parent_span_id and hasattr(span, "parent_span_id"):
            span.parent_span_id = parent_span_id
        try:
            span.set_meta(instrumented_by="langgraph")
        except Exception:  # noqa: BLE001
            logger.debug("LangGraph %s span 标记失败", kind, exc_info=True)
        return span

    def _finish(
        self,
        run_id: str,
        *,
        outputs: Any = None,
        error: BaseException | None = None,
        error_code: str | None = None,
    ) -> None:
        state = self._runs.pop(run_id, None)
        if state is None:
            return
        if state.kind == _KIND_ROOT:
            self._finish_root(outputs, error)
            return
        if state.kind == _KIND_IGNORED:
            return
        if state.span is None or state.reused:
            # 复用的是业务侧 span：由业务埋点负责写 output/error 和收口。
            return
        span = state.span
        if error is not None:
            code = error_code or f"{state.kind}_error"
            try:
                span.set_error(
                    code, f"{type(error).__name__}: {error}", retryable=False
                )
            except Exception:  # noqa: BLE001
                logger.debug("LangGraph span 标注错误失败", exc_info=True)
            self._safe_finish(span, "failed")
            return
        if outputs is not None:
            try:
                span.set_output(outputs)
            except Exception:  # noqa: BLE001
                logger.debug("LangGraph span 记录输出失败", exc_info=True)
        self._safe_finish(span, "succeeded")

    def _finish_root(
        self, outputs: Any, error: BaseException | None
    ) -> None:
        status = "failed" if error is not None else "succeeded"
        shell = self._root_shell
        # 只有 interrupt/resume 事件需要靠根 span.close 落 trace_events；
        # 没有事件时不重写根 span（trace.close 已更新根 span 状态）。
        if shell is not None and self._root_events_pending:
            if error is not None:
                try:
                    shell.set_error(
                        "langgraph_error",
                        f"{type(error).__name__}: {error}",
                        retryable=False,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("根 workflow span 标注错误失败", exc_info=True)
            elif outputs is not None:
                try:
                    shell.set_output(outputs)
                except Exception:  # noqa: BLE001
                    logger.debug("根 workflow span 记录输出失败", exc_info=True)
            self._safe_finish(shell, status)

        trace = self._trace_handle
        if trace is not None and self._owns_trace:
            try:
                if error is None and outputs is not None:
                    trace.set_output(outputs)
                trace.close(status)
            except Exception:  # noqa: BLE001
                logger.warning("LangGraph trace 收口失败", exc_info=True)
        self._trace_handle = None
        self._owns_trace = False
        self._workflow_span_id = None
        self._root_shell = None
        self._root_events_pending = False

    def _add_event(self, event_type: str, payload: dict | None) -> None:
        target = self._event_target()
        if target is None:
            return
        data = payload if isinstance(payload, dict) else (
            {} if payload is None else {"payload": payload}
        )
        target.add_event(event_type, data)
        if target is self._root_shell:
            self._root_events_pending = True

    def _event_target(self) -> Any | None:
        trace = obs_context.current_trace.get()
        current = obs_context.current_span.get()
        if (
            current is not None
            and not isinstance(current, NullSpan)
            and getattr(current, "span_id", "")
        ):
            trace_id = getattr(trace, "trace_id", "") if trace is not None else ""
            if not trace_id or getattr(current, "trace_id", "") == trace_id:
                return current
        return self._ensure_root_shell()

    def _ensure_root_shell(self) -> SpanHandle | None:
        """构造根 workflow span 的内存句柄（不重复写 span.open）。

        正常路径下根 span 由 ``tracer.start_trace`` 打开；这里只持有同一个
        ``span_id`` 的句柄，用于在没有节点 span 时挂 interrupt/resume 事件。
        """
        if self._root_shell is not None:
            return self._root_shell
        trace = self._trace_handle or obs_context.current_trace.get()
        if trace is None or isinstance(trace, NullTrace):
            return None
        root_span_id = getattr(trace, "root_span_id", None)
        if not root_span_id:
            return None
        try:
            self._root_shell = SpanHandle(
                tracer=self._tracer,
                tenant_id=getattr(trace, "tenant_id", None),
                trace_id=getattr(trace, "trace_id", ""),
                span_id=root_span_id,
                parent_span_id=None,
                name=getattr(trace, "name", "workflow.langgraph"),
                span_type="workflow",
                start_time=getattr(trace, "start_time", None)
                or datetime.now(timezone.utc),
            )
        except Exception:  # noqa: BLE001
            logger.debug("根 workflow span 句柄构造失败", exc_info=True)
            return None
        return self._root_shell

    def _current_kind_span(self, kind: str) -> Any | None:
        """当前上下文里同类型的业务 span：用于避免重复建 llm/tool span。"""
        current = obs_context.current_span.get()
        if current is None or isinstance(current, NullSpan):
            return None
        if getattr(current, "span_type", None) != kind:
            return None
        if not getattr(current, "span_id", ""):
            return None
        return current

    def _nearest_ancestor_span(self, parent_run_id: str | None) -> Any | None:
        current = parent_run_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            state = self._runs.get(current)
            if state is None:
                return None
            if (
                state.span is not None
                and state.kind in (_KIND_NODE, _KIND_LLM, _KIND_TOOL)
                and not state.reused
                and getattr(state.span, "span_id", "")
            ):
                return state.span
            current = state.parent
        return None

    def _has_node_ancestor(self, parent_run_id: str | None, node_name: str) -> bool:
        current = parent_run_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            state = self._runs.get(current)
            if state is None:
                return False
            if state.kind == _KIND_NODE and state.node_name == node_name:
                return True
            current = state.parent
        return False

    @staticmethod
    def _safe_finish(span: Any, status: str) -> None:
        try:
            span.finish(status)
        except Exception:  # noqa: BLE001 - 收口失败只记日志
            logger.debug("LangGraph span 收口失败", exc_info=True)


def langgraph_callback(tracer: Any | None = None) -> LangGraphCallback:
    """便捷工厂：``config={"callbacks": [langgraph_callback()]}``。"""
    return LangGraphCallback(tracer=tracer)


__all__ = ["LangGraphCallback", "langgraph_callback"]
