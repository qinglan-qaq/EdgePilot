import random
import secrets
from datetime import datetime, timezone

from app.core.tenancy import TenantNotSetError, get_tenant_id
from app.observability import context
from app.observability.span import NullSpan, NullTrace, SpanHandle, TraceHandle


def _hex32() -> str:
    return secrets.token_hex(16)


def _hex16() -> str:
    return secrets.token_hex(8)


class Tracer:
    def __init__(self, *, enabled: bool, sample_rate: float, collector,
                 capture_mode: str = "REDACTED") -> None:
        self.enabled = enabled
        self.sample_rate = sample_rate
        self.collector = collector
        self.capture_mode = capture_mode
        self._sampled_out = 0

    def emit(self, record: dict) -> bool:
        if not self.enabled:
            return False
        if not self.collector.put_nowait(record):
            from app.observability.metrics import event_dropped

            event_dropped.labels(reason="queue_full").inc()
            return False
        return True

    def start_trace(self, name, *, source="workflow_run", user_id=None, session_id=None,
                    input=None, metadata=None):
        prev_trace = context.current_trace.get()
        prev_span = context.current_span.get()

        def _null(reason: str) -> NullTrace:
            # 即使不采集也要占住上下文：否则内层未采样/无租户的 trace
            # 会把后续 span 错挂到外层 trace 上
            null = NullTrace(reason, prev_trace=prev_trace, prev_span=prev_span)
            context.current_trace.set(null)
            context.current_span.set(None)
            return null

        if not self.enabled or random.random() >= self.sample_rate:
            return _null("disabled")
        try:
            tenant_id = get_tenant_id()
        except TenantNotSetError:
            # 观测不因缺租户而抛异常，但也不会写入任何记录
            return _null("no_tenant")
        trace = TraceHandle(
            tracer=self, tenant_id=tenant_id, trace_id=_hex32(),
            root_span_id=_hex16(), name=name,
            source=source, start_time=datetime.now(timezone.utc),
            _prev_trace=prev_trace, _prev_span=prev_span,
        )
        self.emit({
            "kind": "trace.start",
            "tenant_id": tenant_id,
            "trace_id": trace.trace_id,
            "root_span_id": trace.root_span_id,
            "name": name,
            "source": source,
            "user_id": user_id,
            "session_id": session_id,
            "input": input,
            "metadata": metadata or {},
            "start_time": trace.start_time,
        })
        # 根 span 在 trace 开始时就 INSERT，运行中的 trace 才有可渲染的树根
        self.emit({
            "kind": "span.open",
            "tenant_id": tenant_id,
            "trace_id": trace.trace_id,
            "span_id": trace.root_span_id,
            "parent_span_id": None,
            "name": name,
            "span_type": "workflow",
            "status": "running",
            "attempt": 1,
            "start_time": trace.start_time,
            "input": input,
            "metadata": metadata or {},
        })
        context.current_trace.set(trace)
        context.current_span.set(None)
        context.sequence.set(0)
        return trace

    def start_span(self, name, span_type, *, attempt=1, agent_id=None, agent_version=None,
                   node_id=None, tool_id=None, version=None):
        trace = context.current_trace.get()
        # NullTrace 也要挡掉，否则会被当成"有 trace"而把 span 挂错地方
        if not self.enabled or trace is None or isinstance(trace, NullTrace):
            return NullSpan()
        parent = context.current_span.get()
        span = SpanHandle(
            tracer=self, tenant_id=trace.tenant_id, trace_id=trace.trace_id,
            span_id=_hex16(),
            parent_span_id=(parent.span_id if parent else trace.root_span_id),
            name=name, span_type=span_type,
            start_time=datetime.now(timezone.utc), attempt=attempt,
            agent_id=agent_id, agent_version=agent_version, node_id=node_id,
            tool_id=tool_id, version=version,
            _prev_span=parent,
        )
        context.current_span.set(span)
        return span

    def current_trace_id(self) -> str | None:
        trace = context.current_trace.get()
        return trace.trace_id if trace else None
