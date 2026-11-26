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
        """按采集模式脱敏后投递一条观测记录，永不向业务抛异常。

        关键节点：
            1. 采集关闭或解析为 OFF 时直接丢弃；
            2. 采集器降级时由 ``resolve_mode`` 把模式压到 METADATA；
            3. 队列满只累加 ``event_dropped{queue_full}``，不阻塞业务线程。

        Args:
            record: 已带 tenant_id/trace_id 的观测记录。

        Returns:
            True 表示已进入采集队列；False 表示被丢弃。
        """
        if not self.enabled:
            return False
        from app.observability.capture import resolve_mode
        from app.observability.redaction import redact_record

        mode = resolve_mode(self.capture_mode, degraded=self.collector.degraded)
        if mode == "OFF":
            return False
        if not self.collector.put_nowait(redact_record(record, mode)):
            from app.observability.metrics import event_dropped

            event_dropped.labels(reason="queue_full").inc()
            return False
        return True

    def start_trace(self, name, *, source="workflow_run", user_id=None, session_id=None,
                    input=None, metadata=None):
        """开启一条 trace，并立即写入 trace.start 与根 workflow span。

        关键节点：
            1. 未启用/未采样/无租户时返回 NullTrace，但仍占住上下文，
               防止内层 span 错挂到外层 trace；
            2. 有租户时生成 32 位 trace_id 与 16 位根 span_id（只用于关联）；
            3. 根 span 在 trace 开始时就 open，运行中的 trace 也有可渲染树根。

        Args:
            name: trace 名称，通常为 ``workflow_run.{id}`` 或 ``playground``。
            source: 来源标识，如 ``workflow_run`` / ``playground``。
            user_id: 触发用户，可为 None。
            session_id: 会话标识，可为 None。
            input: trace 输入，按采集模式脱敏后保存。
            metadata: 附加元数据。

        Returns:
            TraceHandle；未采集时为 NullTrace。
        """
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
        # 关键节点 2：先写 trace 头，再写根 span；两者都走同一条采集队列。
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
        # 关键节点 3：根 span 在 trace 开始时就 INSERT，运行中的 trace 才有树根。
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
        # 关键节点 4：最后再切换上下文；即使 emit 丢弃，后续 span 仍挂在正确 trace。
        context.current_trace.set(trace)
        context.current_span.set(None)
        context.sequence.set(0)
        return trace

    def start_span(self, name, span_type, *, attempt=1, agent_id=None, agent_version=None,
                   node_id=None, tool_id=None, version=None):
        """在当前 trace 下开启子 span；未采集时返回 NullSpan 占位。

        关键节点：
            1. 没有有效 trace（None / NullTrace）时不创建 span；
            2. parent 优先取当前 span，否则挂到根 span；
            3. 生成 16 位 span_id，仅用于关联，不编码时间。

        Args:
            name: span 名称，如 ``node.{id}`` / ``prompt.{template}``。
            span_type: 类型，如 agent / tool / condition / approval / prompt。
            attempt: 逻辑执行的第几次尝试，从 1 开始。
            agent_id: 关联 Agent 主键，可为 None。
            agent_version: 关联 Agent 版本号，可为 None。
            node_id: 关联工作流节点 ID，可为 None。
            tool_id: 关联工具标识，可为 None。
            version: 关联 Prompt/Agent 版本，可为 None。

        Returns:
            SpanHandle；未采集时为 NullSpan。
        """
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
