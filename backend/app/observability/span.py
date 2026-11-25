import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_ms() -> float:
    return time.time() * 1000


@dataclass
class SpanHandle:
    tracer: Any
    tenant_id: uuid.UUID
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    span_type: str
    start_time: datetime
    attempt: int = 1
    agent_id: int | None = None
    agent_version: int | None = None
    node_id: str | None = None
    tool_id: str | None = None
    version: str | None = None
    _status: str = "running"
    _input: Any = None
    _output: Any = None
    _error: dict | None = None
    _meta: dict = field(default_factory=dict)
    _events: list[dict] = field(default_factory=list)
    _llm_calls: list = field(default_factory=list)
    _tool_calls: list = field(default_factory=list)
    _retrievals: list = field(default_factory=list)
    _prev_span: Any = None
    _started: float = field(default_factory=_now_ms)

    def set_input(self, value: Any) -> "SpanHandle":
        self._input = value
        return self

    def set_output(self, value: Any) -> "SpanHandle":
        self._output = value
        return self

    def set_error(self, code: str, message: str, *, retryable: bool = False) -> "SpanHandle":
        self._error = {"code": code, "message": message, "retryable": retryable}
        return self

    def set_meta(self, **kv: Any) -> "SpanHandle":
        self._meta.update(kv)
        return self

    def add_event(self, event_type: str, data: dict) -> "SpanHandle":
        self._events.append({"event_type": event_type, "event_data": data})
        return self

    def record_llm_call(self, rec) -> "SpanHandle":
        self._llm_calls.append(rec)
        return self

    def record_tool_call(self, rec) -> "SpanHandle":
        self._tool_calls.append(rec)
        return self

    def record_retrieval(self, rec) -> "SpanHandle":
        self._retrievals.append(rec)
        return self

    def finish(self, status: str = "succeeded") -> None:
        if self._status != "running":
            return
        self._status = status
        self.tracer.emit({
            "kind": "span.close",
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "span_type": self.span_type,
            "status": status,
            "attempt": self.attempt,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "node_id": self.node_id,
            "tool_id": self.tool_id,
            "version": self.version,
            "start_time": self.start_time,
            "duration_ms": _now_ms() - self._started,
            "input": self._input,
            "output": self._output,
            "error": self._error,
            "metadata": self._meta,
            "events": self._events,
            "llm_calls": self._llm_calls,
            "tool_calls": self._tool_calls,
            "retrievals": self._retrievals,
        })
        # 恢复父 span 为当前节点，否则顺序关闭的兄弟 span 会互相挂成父子
        from app.observability import context as obs_context
        if obs_context.current_span.get() is self:
            obs_context.current_span.set(self._prev_span)

    async def __aenter__(self) -> "SpanHandle":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self._status == "running":
            # 调用方已显式 set_error（如 timeout/permission_denied）时保留其错误码，
            # 只有完全没标注过才退化成 unhandled_exception
            if self._error is None:
                self.set_error("unhandled_exception", f"{exc_type.__name__}: {exc}")
            self.finish("failed")
        else:
            self.finish("succeeded")
        return False  # 绝不吞业务异常


class NullSpan:
    trace_id = ""
    span_id = ""

    def set_input(self, value): return self
    def set_output(self, value): return self
    def set_error(self, code, message, *, retryable=False): return self
    def set_meta(self, **kv): return self
    def add_event(self, event_type, data): return self
    def record_llm_call(self, rec): return self
    def record_tool_call(self, rec): return self
    def record_retrieval(self, rec): return self
    def finish(self, status="succeeded"): return None

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb): return False


@dataclass
class TraceHandle:
    tracer: Any
    tenant_id: uuid.UUID
    trace_id: str
    root_span_id: str
    name: str
    source: str
    start_time: datetime
    _output: Any = None
    _status: str = "running"
    _prev_trace: Any = None
    _prev_span: Any = None

    def set_output(self, value: Any) -> "TraceHandle":
        self._output = value
        return self

    def close(self, status: str = "succeeded") -> None:
        if self._status != "running":
            return
        self._status = status
        self.tracer.emit({
            "kind": "trace.close",
            "tenant_id": self.tenant_id,
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "name": self.name,
            "source": self.source,
            "status": status,
            "output": self._output,
            "start_time": self.start_time,
            "duration_ms": _now_ms() - (self.start_time.timestamp() * 1000),
        })
        # trace 结束后把上下文还给外层（嵌套 trace 或复用的 worker 上下文）
        from app.observability import context as obs_context
        if obs_context.current_trace.get() is self:
            obs_context.current_trace.set(self._prev_trace)
            obs_context.current_span.set(self._prev_span)

    async def __aenter__(self) -> "TraceHandle":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is not None and self._status == "running":
            self.close("failed")
            return False
        self.close("succeeded")
        return False


class NullTrace:
    trace_id = ""
    root_span_id = ""

    def __init__(self, reason: str = "disabled", *, prev_trace=None, prev_span=None) -> None:
        self.reason = reason
        self._prev_trace = prev_trace
        self._prev_span = prev_span

    def set_output(self, value): return self

    def close(self, status="succeeded"):
        from app.observability import context as obs_context
        if obs_context.current_trace.get() is self:
            obs_context.current_trace.set(self._prev_trace)
            obs_context.current_span.set(self._prev_span)

    async def __aenter__(self): return self
    async def __aexit__(self, exc_type, exc, tb):
        self.close()
        return False
