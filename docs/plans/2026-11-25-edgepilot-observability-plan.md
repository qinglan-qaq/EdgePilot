# EdgePilot 可观测中心实施计划（P1–P8）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成 Trace / Span / Event 采集与查询、LLM 与 Tool 逐次调用记录、Prompt 模板版本与渲染记录、运行分析与三栏 Trace 详情，使"一次真实 Agent 请求的完整执行过程"可在前端逐节点还原，且观测故障不影响业务。

**Architecture:** 进程内 `asyncio.Queue` + 批量落库的旁路管道；`contextvars` 传播 trace 上下文；`traces/spans` 为执行真相，`llm_calls/tool_calls/retrieval_records` 按 attempt 1:N 挂在 span 上；Prompts 为不可变版本实体，运行记录只存引用 + hash。

**Tech Stack:** Python 3.11 · SQLAlchemy 2.0（asyncpg）· Alembic · PostgreSQL 16 · Redis 7（live 覆盖层 + pub/sub）· SSE · Vue 3 + Element Plus · 无 OTel SDK（P1 只做语义对齐）

**Spec:** `docs/specs/2026-11-25-edgepilot-observability-design.md`（权威；本计划实现它的 §3–§11）

**配套计划:** `docs/plans/2026-11-25-edgepilot-platform-plan.md`（平台侧；`0001_core`、`app/*` 业务代码归它所有）

---

## 计划粒度（先读这一段）

| 范围 | 粒度 |
|---|---|
| **观测 P1–P2**（Task 1–8） | 代码级：失败测试 + 实现 + 命令 + 提交 |
| **P3–P7**（Task 9–17） | 契约级：文件、接口、失败测试、验收已固定；实现代码在进入该 Phase 时按 Task 1–8 的模板展开 |
| **P8** | 仅边界与验收，独立 spec 另写 |

**Gate 规则**：每个 Phase 结束跑 `make verify`；`observability_event_dropped_total{reason="write_failed"}` 在正常负载下必须为 0。

## 与平台计划的交接点

| # | 交接内容 | 依赖方向 | 说明 |
|---|---|---|---|
| H1 | `app.observability.tracer` facade 与 no-op 行为 | **本计划 → 平台** | 平台 Task 9/10/12 会 import 它；`OBS_ENABLED=false` 时必须零开销且 `.trace_id/.span_id` 可读（空串） |
| H2 | `0002_obs_base` 的 `down_revision = "0001_core"` | **平台 Task 5 → 本计划** | 本计划不得修改 `0001`；也不得创建 `0001` |
| H3 | `LLMCallRecord` / `ToolCallRecord` / `RetrievalRecord` | **本计划 → 平台** | 平台构造，本计划持久化 |
| H4 | agent span 的父子关系 | **平台 P3 → 本计划** | 本计划只验证，不写业务 runner |
| H5 | `web/` 前端壳 | **平台 P6 → 本计划** | 本计划只新增观测页面与 store |

## Global Constraints

- 同平台计划的 Python/版本约束；本计划**不新增**运行期依赖（仅测试可用 `pytest-asyncio`、`fakeredis` 可选）。
- `start_span()` / `set_*()` **同步、无 IO、不抛异常**；所有失败只计数。
- `OBS_ENABLED=false` 或采样未命中：返回 no-op 句柄，**不创建 trace、不入队、不写库**。
- 观测表全部带 `tenant_id`；租户从上下文取，不从请求参数取。
- 跨表**不建外键**（含观测 ↔ 业务）；引用完整性由应用层兜底，UI 对缺失显示"记录已过期"。
- 脱敏在**入队前**完成；队列内不得存在明文敏感数据。
- 不得引入 OpenTelemetry SDK / Collector；只对齐 `traceparent` 语义与 span 属性命名。
- 指标名固定（不得改名）：`observability_trace_total`、`observability_span_total`、`observability_span_error_total`、`observability_event_dropped_total{reason}`、`observability_write_latency_seconds`、`llm_request_total`、`llm_token_total`、`llm_request_duration_seconds`、`tool_call_total`、`tool_call_error_total`。
- 观测代码不得 import `app.agents` / `app.workflow` / `app.tools`（单向依赖：业务 → 观测）。

---

## File Structure

```
backend/app/observability/
├─ __init__.py            # 导出 tracer、records、current_trace_id
├─ context.py             # contextvars：当前 trace / span
├─ span.py                # SpanHandle / NullSpan / TraceHandle / NullTrace
├─ tracer.py              # Tracer：start_trace / start_span / 采样
├─ records.py             # LLMCallRecord / ToolCallRecord / RetrievalRecord
├─ collector.py           # EventCollector：有界队列 + 批量
├─ writer.py              # BatchWriter：records → PG
├─ redaction.py           # 脱敏（入队前）
├─ capture.py             # 采集模式解析与降级状态机
├─ live.py                # Redis 覆盖层 + pub/sub
├─ sweeper.py             # 孤儿 trace/span 补偿
├─ metrics.py             # 观测自有指标
├─ propagation.py         # traceparent / Celery / MQTT / 线程池
├─ callback/langgraph_cb.py
├─ prompt/{renderer.py, recorder.py, version_manager.py}
├─ services/{trace_service.py, analytics_service.py}
├─ api/{traces.py, analytics.py, prompts.py, settings.py}
└─ models/{trace.py, span.py, event.py, llm_call.py, tool_call.py, prompt.py, retrieval.py}

backend/migrations/versions/
├─ 0002_obs_base.py              # traces / spans / trace_events
├─ 0003_obs_llm_tool_prompt.py   # llm_calls / tool_calls / prompt_*
├─ 0004_obs_retrieval.py
└─ 0005_evaluation.py            # P8

backend/tests/unit/observability/…    backend/tests/integration/observability/…

web/src/
├─ api/observability.ts
├─ stores/observability.ts
├─ views/observability/{Overview.vue, Traces.vue, TraceDetail.vue, Prompts.vue,
│                       Playground.vue, Tools.vue, Models.vue, Agents.vue, Settings.vue}
└─ components/observability/{SpanTree.vue, SpanTimeline.vue, SpanInspector.vue,
                             JsonViewer.vue, MetricCard.vue, FilterBar.vue}
```

---

## 观测 P1：采集骨架与查询

### Task 1: facade、上下文、no-op 与采集队列

**Files:**
- Create: `backend/app/observability/__init__.py`、`context.py`、`span.py`、`tracer.py`、`records.py`、`collector.py`、`metrics.py`
- Test: `backend/tests/unit/observability/test_tracer_noop.py`、`test_tracer_sampling.py`、`test_collector_backpressure.py`

**Interfaces（平台计划按此调用，签名不可改）:**
- `tracer.start_trace(name, *, source="workflow_run", user_id=None, session_id=None, input=None, metadata=None) -> TraceHandle`
- `tracer.start_span(name, span_type, *, attempt=1, agent_id=None, agent_version=None, node_id=None, tool_id=None, version=None) -> SpanHandle`
- `tracer.current_trace_id() -> str | None`
- `TraceHandle`: `.trace_id`、`.root_span_id`、`.tenant_id`、`.set_output(v)`、`.close(status="succeeded")`、`async with`
- `SpanHandle`: `.trace_id`、`.span_id`、`.tenant_id`、`.set_input(v)`、`.set_output(v)`、`.set_error(code, message, *, retryable=False)`、`.set_meta(**kv)`、`.add_event(event_type, data)`、`.record_llm_call(rec)`、`.record_tool_call(rec)`、`.record_retrieval(rec)`、`.finish(status="succeeded")`、`async with`
- `records.LLMCallRecord(...)`、`records.ToolCallRecord(...)`、`records.RetrievalRecord(...)`（dataclass，字段见 §5.2）
- `collector.put_nowait(record: dict) -> bool`、`collector.pending() -> list[dict]`（取出尚未写入的记录，测试与优雅停机用）、`await collector.start()`、`await collector.stop()`、`collector.dropped: dict[str,int]`

**两条硬规则：**
1. **记录自带 `tenant_id`。** 每一条入队记录都必须在字段里带 `tenant_id`，因为后台写入任务运行在独立上下文里，此时 `get_tenant_id()` 不可用。`start_trace` 在开始处读一次租户并向下传递。
2. **无租户不产生记录。** `start_trace` 取不到租户时返回 `NullTrace(reason="no_tenant")` 而不抛异常（观测不得影响业务），但也不会入队任何记录。

**前置依赖：** `app/core/tenancy.py` 提供 `set_tenant_id(uuid) -> Token`、`reset_tenant_id(token)`、`get_tenant_id() -> uuid.UUID`、`TenantNotSetError`。该文件由平台计划 Task 3 首次落地；若单独执行本计划，先按下面骨架创建它（内容与平台计划一致，不冲突）。

```python
# backend/app/core/tenancy.py
import uuid
from contextvars import ContextVar

_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("tenant_id", default=None)


class TenantNotSetError(RuntimeError):
    """在未设置租户上下文时访问租户。"""


def set_tenant_id(value: uuid.UUID):
    return _tenant_id.set(value)


def reset_tenant_id(token) -> None:
    _tenant_id.reset(token)


def get_tenant_id() -> uuid.UUID:
    value = _tenant_id.get()
    if value is None:
        raise TenantNotSetError("tenant context is not set")
    return value
```

- [ ] **Step 1: 写失败测试（no-op、采样、无租户）**

```python
# backend/tests/unit/observability/test_tracer_noop.py
import asyncio
import uuid

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.collector import EventCollector
from app.observability.tracer import Tracer

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakeWriter:
    def __init__(self):
        self.batches = []

    async def write(self, records):
        self.batches.append(list(records))


def make_tracer(enabled: bool, sample_rate: float = 1.0):
    writer = FakeWriter()
    collector = EventCollector(maxsize=10, batch_size=100, flush_ms=1000, writer=writer)
    return Tracer(enabled=enabled, sample_rate=sample_rate, collector=collector), collector, writer


def _run(coro):
    """asyncio.run 会复制当前 context，租户在进协程前设置即可见。"""
    token = set_tenant_id(TENANT)
    try:
        return asyncio.run(coro)
    finally:
        reset_tenant_id(token)


def test_disabled_tracer_produces_no_records():
    t, collector, writer = make_tracer(enabled=False)

    async def scenario():
        async with t.start_trace("demo") as trace:
            async with t.start_span("work", "agent") as span:
                span.set_input({"a": 1})
                span.set_output({"b": 2})
            return trace.trace_id, span.span_id

    trace_id, span_id = _run(scenario())
    assert trace_id == ""
    assert span_id == ""
    assert collector.pending() == []
    assert writer.batches == []


def test_no_tenant_returns_null_trace_without_records():
    t, collector, _ = make_tracer(enabled=True)

    async def scenario():
        async with t.start_trace("demo") as trace:
            async with t.start_span("work", "agent"):
                pass
            return trace.reason

    assert asyncio.run(scenario()) == "no_tenant"
    assert collector.pending() == []


def test_enabled_tracer_enqueues_start_open_and_close():
    t, collector, _ = make_tracer(enabled=True)

    async def scenario():
        async with t.start_trace("demo", source="workflow_run"):
            async with t.start_span("work", "agent"):
                pass

    _run(scenario())
    records = collector.pending()
    kinds = [r["kind"] for r in records]
    assert kinds.count("trace.start") == 1
    assert kinds.count("span.open") == 1     # 根 span 在 trace 开始时就落一行，便于实时树
    assert kinds.count("span.close") == 1
    assert kinds.count("trace.close") == 1
    assert {r["tenant_id"] for r in records} == {TENANT}


def test_sibling_spans_attach_to_root_and_restore_context():
    t, collector, _ = make_tracer(enabled=True)

    async def scenario():
        async with t.start_trace("demo") as trace:
            async with t.start_span("a", "agent"):
                pass
            async with t.start_span("b", "tool"):
                pass
        return trace.root_span_id

    root = _run(scenario())
    closes = [r for r in collector.pending() if r["kind"] == "span.close"]
    # 兄弟 span 都挂在根下；若关闭后不恢复上下文，b 会被挂到 a 下面
    assert [r["parent_span_id"] for r in closes] == [root, root]
    assert [r["name"] for r in closes] == ["a", "b"]


def test_get_tracer_follows_obs_enabled_setting(monkeypatch):
    import app.observability as obs
    from app.core.config import get_settings

    monkeypatch.setenv("OBS_ENABLED", "false")
    get_settings.cache_clear()
    obs.reset_tracer()
    try:
        assert obs.get_tracer().enabled is False
    finally:
        obs.reset_tracer()
        get_settings.cache_clear()
```

```python
# backend/tests/unit/observability/test_tracer_sampling.py
import asyncio
import uuid

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.collector import EventCollector
from app.observability.tracer import Tracer


def test_sample_rate_zero_skips_everything():
    collector = EventCollector(maxsize=10, batch_size=100, flush_ms=1000, writer=None)
    t = Tracer(enabled=True, sample_rate=0.0, collector=collector)

    async def scenario():
        async with t.start_trace("demo") as trace:
            async with t.start_span("work", "agent"):
                pass
        return trace.trace_id

    token = set_tenant_id(uuid.uuid4())
    try:
        assert asyncio.run(scenario()) == ""
    finally:
        reset_tenant_id(token)
    assert collector.pending() == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && pytest tests/unit/observability -v`
Expected: FAIL（`No module named 'app.observability'`）

- [ ] **Step 3: 实现 records 与 context**

```python
# backend/app/observability/records.py
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class LLMCallRecord:
    provider: str
    model_name: str
    model_version: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tool_definitions: list[dict] | None = None
    tool_choice: str | None = None
    request_messages: list[dict] | None = None
    raw_response: dict | None = None
    structured_response: dict | None = None
    response_format: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    cost: Decimal | None = None
    request_id: str | None = None
    error: dict | None = None


@dataclass
class ToolCallRecord:
    tool_id: str
    tool_name: str
    tool_version: str = "1.0.0"
    description: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    input_arguments: dict | None = None
    output_result: Any = None
    execution_status: str = "succeeded"
    permission_result: str = "granted"   # granted | denied | approval_required
    duration_ms: float | None = None
    retry_count: int = 0
    error: dict | None = None


@dataclass
class RetrievalRecord:
    query: str
    rewritten_query: str | None = None
    retrieval_strategy: str | None = None
    embedding_model: str | None = None
    vector_store: str | None = None
    reranker_model: str | None = None
    top_k: int | None = None
    similarity_threshold: float | None = None
    retrieved_documents: list[dict] = field(default_factory=list)
    reranked_documents: list[dict] = field(default_factory=list)
    final_context: str | None = None
    duration_ms: float | None = None
    error: dict | None = None
```

```python
# backend/app/observability/context.py
from contextvars import ContextVar

from app.observability.span import SpanHandle, TraceHandle

current_trace: ContextVar[TraceHandle | None] = ContextVar("obs_current_trace", default=None)
current_span: ContextVar[SpanHandle | None] = ContextVar("obs_current_span", default=None)
sequence: ContextVar[int] = ContextVar("obs_sequence", default=0)


def next_sequence() -> int:
    value = sequence.get() + 1
    sequence.set(value)
    return value
```

- [ ] **Step 4: 实现 span / tracer / collector / metrics**

```python
# backend/app/observability/span.py
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
```

```python
# backend/app/observability/tracer.py
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
```

```python
# backend/app/observability/collector.py
import asyncio


class EventCollector:
    def __init__(self, *, maxsize: int, batch_size: int, flush_ms: int, writer,
                 retry_delays: tuple[float, ...] = (0.2, 1.0)) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self._batch_size = batch_size
        self._flush_s = flush_ms / 1000
        self._writer = writer
        self._retry_delays = retry_delays
        self._task: asyncio.Task | None = None
        self.dropped = {"queue_full": 0, "write_failed": 0}
        self.degraded = False
        self._consecutive_failures = 0

    def set_writer(self, writer) -> None:
        self._writer = writer

    def qsize(self) -> int:
        return self._queue.qsize()

    def pending(self) -> list[dict]:
        """取出尚未写入的记录（测试与优雅停机用；生产写路径不走这里）。"""
        items: list[dict] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    def put_nowait(self, record: dict) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self.dropped["queue_full"] += 1
            return False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 停机收尾：把队列里剩下的记录写完，避免最后一批丢失
        while True:
            rest = self.pending()
            if not rest:
                return
            await self._write_with_retry(rest)

    async def _run(self) -> None:
        while True:
            batch: list[dict] = []
            deadline = asyncio.get_running_loop().time() + self._flush_s
            while len(batch) < self._batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                except asyncio.TimeoutError:
                    break
            if batch:
                await self._write_with_retry(batch)

    async def _write_with_retry(self, batch: list[dict]) -> None:
        for attempt in range(3):
            try:
                await self._writer.write(batch)
                self._consecutive_failures = 0
                if self.degraded:
                    self.degraded = False  # 写入恢复后自动退出降级
                return
            except Exception:
                if attempt == 2:
                    self.dropped["write_failed"] += len(batch)
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 5:
                        # 连续 5 批写不进去 → 降到 METADATA（只留结构，不存正文）
                        self.degraded = True
                    from app.observability.metrics import event_dropped

                    event_dropped.labels(reason="write_failed").inc(len(batch))
                    return
                await asyncio.sleep(self._retry_delays[attempt])
```

```python
# backend/app/observability/metrics.py
from prometheus_client import Counter, Histogram

trace_total = Counter("observability_trace_total", "Trace 总数", ["source", "status"])
span_total = Counter("observability_span_total", "Span 总数", ["span_type", "status"])
span_error_total = Counter("observability_span_error_total", "Span 错误数", ["span_type", "code"])
event_dropped = Counter("observability_event_dropped_total", "观测数据丢弃数", ["reason"])
write_latency = Histogram("observability_write_latency_seconds", "观测批量写入耗时")
llm_request_total = Counter("llm_request_total", "LLM 调用数", ["provider", "model"])
llm_token_total = Counter("llm_token_total", "LLM token 数", ["provider", "model", "kind"])
llm_request_duration_seconds = Histogram("llm_request_duration_seconds", "LLM 请求耗时",
                                         ["provider", "model"])
tool_call_total = Counter("tool_call_total", "工具调用数", ["tool_id", "status"])
tool_call_error_total = Counter("tool_call_error_total", "工具调用失败数", ["tool_id", "code"])
llm_duration = Histogram("llm_request_duration_seconds", "LLM 调用耗时", ["provider", "model"])
tool_call_total = Counter("tool_call_total", "工具调用数", ["tool_id", "status"])
tool_call_error_total = Counter("tool_call_error_total", "工具调用错误数", ["tool_id", "code"])
```

```python
# backend/app/observability/__init__.py
"""观测层的进程级入口。业务代码只从这里拿 tracer。"""
from app.observability.collector import EventCollector
from app.observability.tracer import Tracer

_tracer: Tracer | None = None


class NullWriter:
    """观测 P2 之前的占位写入器：记录被丢弃，不影响业务。"""

    async def write(self, records) -> None:
        return None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        from app.core.config import get_settings

        s = get_settings()
        _tracer = Tracer(
            enabled=s.obs_enabled,
            sample_rate=s.obs_sample_rate,
            capture_mode=s.obs_capture_mode,
            collector=EventCollector(
                maxsize=s.obs_queue_size,
                batch_size=s.obs_batch_size,
                flush_ms=s.obs_flush_ms,
                writer=NullWriter(),
            ),
        )
    return _tracer


def set_writer(writer) -> None:
    """观测 P2 用真实 BatchWriter 替换 NullWriter；测试也用它注入 FakeWriter。"""
    get_tracer().collector.set_writer(writer)


def reset_tracer() -> None:
    global _tracer
    _tracer = None
```

- [ ] **Step 5: 补背压测试并运行**

```python
# backend/tests/unit/observability/test_collector_backpressure.py
import asyncio
import pytest

from app.observability.collector import EventCollector


class FakeWriter:
    def __init__(self):
        self.batches = []

    async def write(self, records):
        self.batches.append(list(records))


def test_queue_full_drops_without_raising():
    c = EventCollector(maxsize=1, batch_size=10, flush_ms=1000, writer=FakeWriter())
    assert c.put_nowait({"i": 1}) is True
    assert c.put_nowait({"i": 2}) is False
    assert c.dropped["queue_full"] == 1


@pytest.mark.asyncio
async def test_flushes_by_batch_size():
    writer = FakeWriter()
    c = EventCollector(maxsize=100, batch_size=3, flush_ms=5000, writer=writer)
    await c.start()
    for i in range(3):
        c.put_nowait({"i": i})
    await asyncio.sleep(0.05)
    await c.stop()
    assert [len(b) for b in writer.batches] == [3]


@pytest.mark.asyncio
async def test_write_failure_is_counted_not_raised():
    class Broken:
        async def write(self, records):
            raise RuntimeError("pg down")

    c = EventCollector(maxsize=10, batch_size=1, flush_ms=50, writer=Broken())
    await c.start()
    c.put_nowait({"i": 0})
    await asyncio.sleep(1.5)
    await c.stop()
    assert c.dropped["write_failed"] == 1


@pytest.mark.asyncio
async def test_five_failed_batches_degrade_then_recover():
    class Flaky:
        def __init__(self):
            self.fail = True

        async def write(self, records):
            if self.fail:
                raise RuntimeError("pg down")

    writer = Flaky()
    c = EventCollector(maxsize=10, batch_size=1, flush_ms=10, writer=writer,
                       retry_delays=(0, 0))
    for _ in range(5):
        await c._write_with_retry([{"i": 0}])
    assert c.degraded is True
    assert c.dropped["write_failed"] == 5

    writer.fail = False
    await c._write_with_retry([{"i": 1}])
    assert c.degraded is False
```

Run: `cd backend && pytest tests/unit/observability -v`
Expected: 10 passed（noop 5 + 采样 1 + 背压 4；背压含连续失败降级与恢复）

- [ ] **Step 6: 提交**

```bash
git add backend/app/observability backend/tests/unit/observability
git commit -m "feat(obs): tracer facade、no-op 采样与有界采集队列"
```

---

### Task 2: `0002_obs_base` 迁移（traces / spans / trace_events）

**Files:**
- Create: `backend/migrations/versions/0002_obs_base.py`、`backend/app/observability/models/{trace.py, span.py, event.py}`
- Test: `backend/tests/integration/observability/test_obs_schema.py`

**Interfaces:**
- Produces: 三张表；`down_revision = "0001_core"`（H2）
- 字段与索引严格按 spec §5.2/§5.3；`tenant_id uuid not null`；**不建**到业务表的外键。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/observability/test_obs_schema.py
import pytest
from sqlalchemy import text

TABLES = {"traces", "spans", "trace_events"}
REQUIRED = {
    "traces": {"trace_id", "tenant_id", "name", "source", "user_id", "session_id",
               "root_span_id", "status", "input", "output", "start_time", "end_time",
               "duration_ms", "total_tokens", "total_cost", "cost_unavailable_count",
               "total_spans", "error_count", "metadata", "created_at"},
    "spans": {"span_id", "tenant_id", "trace_id", "parent_span_id", "name", "span_type",
              "agent_id", "agent_version", "workflow_id", "node_id", "tool_id", "status",
              "attempt", "start_time", "end_time", "duration_ms", "input", "output",
              "error", "retry_count", "metadata", "links", "created_at"},
    "trace_events": {"tenant_id", "trace_id", "span_id", "event_type", "event_data",
                     "sequence_number", "created_at"},
}


@pytest.mark.asyncio
async def test_obs_tables_and_columns(session):
    for table, cols in REQUIRED.items():
        rows = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"), {"t": table})
        actual = {r[0] for r in rows}
        assert cols <= actual, f"{table} 缺少 {cols - actual}"


@pytest.mark.asyncio
async def test_obs_tables_are_tenant_scoped_and_have_no_business_fk(session):
    rows = await session.execute(text("""
        SELECT c.table_name, c.column_name FROM information_schema.columns c
        WHERE c.table_schema='public' AND c.column_name='tenant_id'
    """))
    assert TABLES <= {r[0] for r in rows}

    fks = await session.execute(text("""
        SELECT tc.table_name FROM information_schema.table_constraints tc
        WHERE tc.table_schema='public' AND tc.constraint_type='FOREIGN KEY'
          AND tc.table_name = ANY(:tables)
    """), {"tables": list(TABLES)})
    assert list(fks) == [], "观测表不得有外键"


@pytest.mark.asyncio
async def test_trace_events_sequence_is_unique_per_trace(session):
    ddl = await session.execute(text("""
        SELECT indexdef FROM pg_indexes
        WHERE schemaname='public' AND tablename='trace_events'
    """))
    assert any("unique" in (row[0] or "").lower() and "sequence_number" in row[0] for row in ddl)
```

- [ ] **Step 2: 运行，确认失败；Step 3: 实现迁移**

`0002_obs_base.py` 的 `down_revision = "0001_core"`。建表要点：

```python
op.create_table(
    "traces",
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("trace_id", sa.String(32), nullable=False),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("user_id", sa.String(64)),
    sa.Column("session_id", sa.String(64)),
    sa.Column("root_span_id", sa.String(16)),
    sa.Column("status", sa.String(24), nullable=False, server_default="running"),
    sa.Column("input", postgresql.JSONB),
    sa.Column("output", postgresql.JSONB),
    sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("end_time", sa.DateTime(timezone=True)),
    sa.Column("duration_ms", sa.Double),
    sa.Column("total_tokens", sa.BigInteger),
    sa.Column("total_cost", sa.Numeric(12, 6)),
    sa.Column("cost_unavailable_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("total_spans", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("metadata", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
op.create_index("uq_traces_trace_id", "traces", ["trace_id"], unique=True)
op.create_index("ix_traces_tenant_start", "traces", ["tenant_id", "start_time"])
op.create_index("ix_traces_tenant_status_start", "traces", ["tenant_id", "status", "start_time"])
op.create_index("ix_traces_metadata_gin", "traces", ["metadata"], postgresql_using="gin")

op.create_table(
    "spans",
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("span_id", sa.String(16), nullable=False),
    sa.Column("trace_id", sa.String(32), nullable=False),
    sa.Column("parent_span_id", sa.String(16)),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("span_type", sa.String(24), nullable=False),
    sa.Column("agent_id", sa.BigInteger),
    sa.Column("agent_version", sa.Integer),
    sa.Column("workflow_id", sa.BigInteger),
    sa.Column("node_id", sa.String(64)),
    sa.Column("tool_id", sa.String(64)),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
    sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
    sa.Column("end_time", sa.DateTime(timezone=True)),
    sa.Column("duration_ms", sa.Double),
    sa.Column("input", postgresql.JSONB),
    sa.Column("output", postgresql.JSONB),
    sa.Column("error", postgresql.JSONB),
    sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("metadata", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
    sa.Column("links", postgresql.JSONB),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
op.create_index("uq_spans_span_id", "spans", ["span_id"], unique=True)
op.create_index("ix_spans_trace_start", "spans", ["trace_id", "start_time"])
op.create_index("ix_spans_tenant_type_start", "spans", ["tenant_id", "span_type", "start_time"])
op.create_index("ix_spans_tenant_agent_start", "spans", ["tenant_id", "agent_id", "start_time"])
op.create_index("ix_spans_tenant_tool_start", "spans", ["tenant_id", "tool_id", "start_time"])

op.create_table(
    "trace_events",
    sa.Column("id", sa.BigInteger, primary_key=True),
    sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
    sa.Column("trace_id", sa.String(32), nullable=False),
    sa.Column("span_id", sa.String(16)),
    sa.Column("event_type", sa.String(32), nullable=False),
    sa.Column("event_data", postgresql.JSONB),
    sa.Column("sequence_number", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
)
op.create_index("uq_events_trace_seq", "trace_events", ["trace_id", "sequence_number"], unique=True)
op.create_index("ix_events_trace_created", "trace_events", ["trace_id", "created_at"])
```

- [ ] **Step 4: 运行并提交**

Run: `make migrate && cd backend && pytest tests/integration/observability/test_obs_schema.py -v`
Expected: 3 passed

```bash
git add backend/migrations/versions/0002_obs_base.py backend/app/observability/models backend/tests/integration/observability
git commit -m "feat(obs): 0002_obs_base 迁移（traces/spans/trace_events）"
```

---

### Task 3: Trace 查询 API 与树构建

**Files:**
- Create: `backend/app/observability/services/trace_service.py`、`backend/app/observability/api/{traces.py,__init__.py}`、`backend/app/observability/schemas.py`
- Modify: `backend/app/api/v1/router.py`（挂载 `observability_router`）
- Test: `backend/tests/unit/observability/test_build_tree.py`、`backend/tests/integration/observability/test_trace_api.py`、`backend/tests/integration/observability/conftest.py`（`seeded_trace` 夹具）

**Interfaces:**
- Produces: `build_tree(spans: list[dict]) -> list[dict]`（纯函数，孤儿节点提升为根并标 `orphan=True`）；`GET /api/v1/observability/traces`、`/traces/{trace_id}`、`/traces/{trace_id}/tree`、`/spans/{span_id}`、`/spans/{span_id}/children`
- 权限：`viewer` 可读；租户过滤走 `TenantContext`（平台 Task 3）

- [ ] **Step 1: 写失败测试（纯函数）**

```python
# backend/tests/unit/observability/test_build_tree.py
from app.observability.services.trace_service import build_tree


def span(span_id, parent, start="2026-11-25T10:00:00", **kw):
    return {"span_id": span_id, "parent_span_id": parent, "name": span_id,
            "start_time": start, "span_type": kw.get("span_type", "agent"), **kw}


def test_parent_child_assembly():
    tree = build_tree([span("a", None), span("b", "a"), span("c", "a")])
    assert len(tree) == 1
    assert [n["span_id"] for n in tree[0]["children"]] == ["b", "c"]


def test_orphan_becomes_root_with_flag():
    tree = build_tree([span("a", None), span("x", "missing-parent")])
    assert {n["span_id"] for n in tree} == {"a", "x"}
    orphan = next(n for n in tree if n["span_id"] == "x")
    assert orphan["orphan"] is True


def test_parallel_children_sorted_by_start_time():
    tree = build_tree([
        span("root", None),
        span("late", "root", "2026-11-25T10:00:05"),
        span("early", "root", "2026-11-25T10:00:01"),
    ])
    assert [c["span_id"] for c in tree[0]["children"]] == ["early", "late"]
```

- [ ] **Step 2–3: 实现纯函数与查询服务**

```python
# backend/app/observability/services/trace_service.py
def build_tree(spans: list[dict]) -> list[dict]:
    nodes = {s["span_id"]: {**s, "children": [], "orphan": False} for s in spans}
    roots: list[dict] = []
    for node in nodes.values():
        parent_id = node.get("parent_span_id")
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            node["orphan"] = bool(parent_id)
            roots.append(node)

    def key(n):
        return (str(n.get("start_time") or ""), n["span_id"])

    for node in nodes.values():
        node["children"].sort(key=key)
    roots.sort(key=key)
    return roots
```

`trace_service.list_traces(filter)`: 按 `tenant_id` + 时间 + 状态 + `trace_id` 前缀 + `slow`（`duration_ms >= 阈值`）分页；`tree(trace_id)`: 查 `spans` → `build_tree`；`span_detail(span_id)`: span + 关联 `llm_calls/tool_calls/retrieval_records`（表不存在时返回空列表，不报错，便于 Task 6 之前联调）。

- [ ] **Step 4: 写 API 集成测试并运行**

```python
# backend/tests/integration/observability/conftest.py
import pytest
from sqlalchemy import text

from tests.conftest import TENANT

TRACE_ID = "a" * 32
ROOT_ID = "b" * 16
AGENT_ID = "c" * 16


@pytest.fixture
async def seeded_trace(session):
    """直接插表：不经过 tracer，查询 API 的测试不依赖采集管道是否已就绪。"""
    await session.execute(text(
        "INSERT INTO traces (trace_id, tenant_id, name, source, status, total_spans,"
        " start_time) VALUES (:t, :ten, 'demo', 'test', 'succeeded', 2, now())"),
        {"t": TRACE_ID, "ten": TENANT})
    for span_id, parent, name, span_type in [
        (ROOT_ID, None, "wf", "workflow"),
        (AGENT_ID, ROOT_ID, "agent.device", "agent"),
    ]:
        await session.execute(text(
            "INSERT INTO spans (span_id, tenant_id, trace_id, parent_span_id, name,"
            " span_type, status, start_time)"
            " VALUES (:s, :ten, :t, :p, :n, :ty, 'succeeded', now())"),
            {"s": span_id, "ten": TENANT, "t": TRACE_ID, "p": parent,
             "n": name, "ty": span_type})
    return TRACE_ID
```

```python
# backend/tests/integration/observability/test_trace_api.py
import pytest

from tests.conftest import auth_headers


@pytest.mark.asyncio
async def test_list_and_tree(client, viewer_token, seeded_trace):
    r = await client.get("/api/v1/observability/traces", headers=auth_headers(viewer_token))
    assert r.json()["code"] == 0
    assert r.json()["data"]["total"] >= 1

    tree = await client.get(f"/api/v1/observability/traces/{seeded_trace}/tree",
                            headers=auth_headers(viewer_token))
    roots = tree.json()["data"]
    assert roots[0]["span_type"] == "workflow"
    assert any(c["span_type"] == "agent" for c in roots[0]["children"])


@pytest.mark.asyncio
async def test_cross_tenant_trace_is_404(client, other_tenant_token, seeded_trace):
    r = await client.get(f"/api/v1/observability/traces/{seeded_trace}",
                         headers=auth_headers(other_tenant_token))
    assert r.status_code == 404
```

- [ ] **Step 5: 运行并提交**

Run: `cd backend && pytest tests/unit/observability/test_build_tree.py tests/integration/observability/test_trace_api.py -v`
Expected: 5 passed（树构建 3 + API 2）

```bash
git add backend/app/observability backend/app/api/v1/router.py backend/tests
git commit -m "feat(obs): Trace 查询 API 与执行树构建"
```

---

### Task 4: 实时覆盖层、SSE 与孤儿补偿

**Files:**
- Create: `backend/app/observability/live.py`、`backend/app/observability/sweeper.py`、`backend/app/observability/sse.py`、`backend/app/observability/api/traces.py`（追加 SSE 端点）
- Test: `backend/tests/integration/observability/test_sweeper.py`、`backend/tests/integration/observability/test_sse_stream.py`、`backend/tests/integration/observability/conftest.py`（追加 `fake_pubsub` 夹具）

**Interfaces:**
- Produces: `LiveLayer.publish(trace_id, payload)`、`LiveLayer.snapshot(trace_id) -> dict`、`LiveLayer.subscribe(trace_id) -> AsyncIterator[dict]`；`sweep_orphans(session, now, timeout_minutes=10) -> int`；`format_event(sequence_number: int, payload: dict) -> str`；`stream_trace_events(session_factory, pubsub, trace_id, last_event_id=None) -> AsyncIterator[str]`
- SSE：`GET /traces/{trace_id}/stream`，事件 id = `sequence_number`，支持 `Last-Event-ID` 从 PG 补发。

- [ ] **Step 1: 写失败测试（孤儿扫描，纯 SQL 可测）**

```python
# backend/tests/integration/observability/test_sweeper.py
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.sweeper import sweep_orphans


@pytest.mark.asyncio
async def test_sweep_marks_stale_running_rows_timeout(session):
    import uuid

    tid = uuid.uuid4()
    token = set_tenant_id(tid)
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        await session.execute(text("""
            INSERT INTO traces (tenant_id, trace_id, name, source, status, start_time)
            VALUES (:t, 'deadbeef', 'x', 'workflow_run', 'running', :s)
        """), {"t": tid, "s": old})
        await session.commit()

        assert await sweep_orphans(session, timeout_minutes=10) == 1
        status = await session.scalar(text(
            "SELECT status FROM traces WHERE trace_id='deadbeef'"))
        assert status == "timeout"
    finally:
        reset_tenant_id(token)
```

- [ ] **Step 2: 写失败测试（SSE 补发，不依赖真实 Redis）**

```python
# backend/tests/integration/observability/conftest.py
import pytest


class FakePubSub:
    def __init__(self):
        self.channels = []
        self.closed = False

    async def subscribe(self, channel):
        self.channels.append(channel)

    async def listen(self):
        if False:          # 空异步生成器：没有实时消息时立刻结束
            yield

    async def unsubscribe(self, channel):
        pass

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_pubsub():
    return FakePubSub()
```

```python
# backend/tests/integration/observability/test_sse_stream.py
import json
import uuid

import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.sse import format_event, stream_trace_events


def test_format_event_uses_sequence_as_id():
    chunk = format_event(7, {"event_type": "tool_started", "event_data": {"tool": "q"}})
    assert chunk.startswith("id: 7\n")
    assert chunk.endswith("\n\n")
    payload = json.loads(chunk.split("data: ", 1)[1].strip())
    assert payload["event_type"] == "tool_started"


@pytest.mark.asyncio
async def test_stream_replays_events_after_last_event_id(session, session_factory, fake_pubsub):
    tid = uuid.uuid4()
    token = set_tenant_id(tid)
    try:
        await session.execute(text("""
            INSERT INTO traces (tenant_id, trace_id, name, source, status, start_time)
            VALUES (:t, 'trace-a', 'demo', 'workflow_run', 'running', now())
        """), {"t": tid})
        for seq, etype in ((1, "agent_started"), (2, "tool_started")):
            await session.execute(text("""
                INSERT INTO trace_events (tenant_id, trace_id, span_id, event_type,
                                          event_data, sequence_number, created_at)
                VALUES (:t, 'trace-a', NULL, :e, '{}'::jsonb, :s, now())
            """), {"t": tid, "e": etype, "s": seq})
        await session.commit()

        gen = stream_trace_events(session_factory, fake_pubsub, "trace-a", last_event_id=1)
        first = await gen.__anext__()
        await gen.aclose()
    finally:
        reset_tenant_id(token)

    assert "id: 2" in first      # 只补发 Last-Event-ID 之后的事件
    assert "tool_started" in first
    assert "agent_started" not in first
```

- [ ] **Step 3–4: 实现 live / sweeper / sse**

`sweep_orphans`：`UPDATE traces SET status='timeout', end_time=now(), error_count=error_count+1 WHERE status='running' AND start_time < now()-interval`；span 同理，`error` 写 `{"code":"orphaned"}`；返回受影响行数之和。用 `RETURNING id` 计数。

`format_event`: `f"id: {seq}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"`。

`stream_trace_events`: 首次先按 `(tenant_id, trace_id, sequence_number > last_event_id)` 顺序补发 PG 中的 `trace_events`，然后 `await pubsub.subscribe(f"obs:{trace_id}")` 转发实时消息；每 15s 无消息时产出 `": ping\n\n"`；收到 `kind == "trace.close"` 后产出 `event: done\ndata: {}\n\n` 并返回。HTTP 端点用 `StreamingResponse(media_type="text/event-stream")`，并从 `Last-Event-ID` 头读取续传位置。

- [ ] **Step 5: 运行并提交**

Run: `cd backend && pytest tests/integration/observability/test_sweeper.py tests/integration/observability/test_sse_stream.py -v`
Expected: 全部 passed

```bash
git commit -m "feat(obs): Redis 实时层、SSE 流与孤儿记录补偿"
```

---

### Task 5: 采集模式与脱敏

**Files:**
- Create: `backend/app/observability/redaction.py`、`backend/app/observability/capture.py`
- Modify: `backend/app/observability/tracer.py`（入队前过 `redact_record`）
- Test: `backend/tests/unit/observability/test_redaction.py`、`backend/tests/unit/observability/test_capture_mode.py`

**Interfaces:**
- Produces: `redact(value, mode) -> Any`、`redact_record(record: dict, mode: str) -> dict`、`resolve_mode(configured: str, tenant_override: str | None = None, *, degraded: bool = False) -> str`
- 模式：`OFF | METADATA | REDACTED | FULL`；`OFF` 在 tracer 层直接短路（不入队）。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/observability/test_redaction.py
from app.observability.redaction import redact, redact_record, SENSITIVE_KEY_RE


def test_masks_sensitive_keys_recursively():
    payload = {"user": {"email": "a@b.com", "password": "x"},
               "headers": {"Authorization": "Bearer abc"}}
    out = redact(payload, "REDACTED")
    assert out["user"]["email"] == "***"
    assert out["user"]["password"] == "***"
    assert out["headers"]["Authorization"] == "***"


def test_masks_patterns_inside_plain_strings():
    out = redact({"note": "key=sk-abcdef123456 phone=13800138000"}, "REDACTED")
    assert "sk-abcdef123456" not in out["note"]
    assert "13800138000" not in out["note"]


def test_full_mode_passthrough_and_metadata_mode_empty():
    payload = {"api_key": "sk-1", "text": "hello"}
    assert redact(payload, "FULL") == payload
    assert redact(payload, "METADATA") is None


def test_record_redaction_keeps_structure_and_drops_bodies_in_metadata():
    rec = {"kind": "trace.start", "input": {"api_key": "sk-1", "q": "hi"}}
    out = redact_record(rec, "METADATA")
    assert out["input"] is None
    assert out["kind"] == "trace.start"

    out2 = redact_record(rec, "REDACTED")
    assert out2["input"]["api_key"] == "***"
    assert out2["input"]["q"] == "hi"


def test_token_counts_ids_and_timestamps_are_never_masked():
    """计数字段/主键/时间戳是观测数据本身，脱敏损坏它们等于丢数据。"""
    rec = {"tenant_id": "00000000-0000-0000-0000-000000000001",
           "trace_id": "a" * 32, "span_id": "b" * 16,
           "start_time": "2026-11-25T00:00:00Z",
           "kind": "llm.close",
           "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                     "total_tokens": 20, "cached_tokens": 0},
           "total_tokens": 20, "total_cost": None,
           "api_key": "sk-should-be-masked"}
    out = redact_record(rec, "REDACTED")
    assert out["tenant_id"] == rec["tenant_id"]
    assert out["trace_id"] == rec["trace_id"]
    assert out["span_id"] == rec["span_id"]
    assert out["start_time"] == rec["start_time"]
    assert out["usage"]["total_tokens"] == 20
    assert out["total_tokens"] == 20
    assert out["total_cost"] is None
    assert out["api_key"] == "***"
```

```python
# backend/tests/unit/observability/test_capture_mode.py
from app.observability.capture import resolve_mode


def test_off_wins_over_tenant_override_and_degrade():
    assert resolve_mode("OFF", tenant_override="FULL", degraded=True) == "OFF"


def test_invalid_value_falls_back_to_redacted():
    assert resolve_mode("bogus") == "REDACTED"


def test_degrade_downgrades_content_modes_to_metadata():
    assert resolve_mode("FULL", degraded=True) == "METADATA"
    assert resolve_mode("REDACTED", degraded=True) == "METADATA"
    assert resolve_mode("METADATA", degraded=True) == "METADATA"


def test_tenant_override_applies_only_when_not_off():
    assert resolve_mode("METADATA", tenant_override="FULL") == "FULL"
    assert resolve_mode("METADATA", tenant_override="OFF") == "METADATA"
```

- [ ] **Step 2–4: 实现**

```python
# backend/app/observability/redaction.py
import re

# 注意排除 *_tokens 计数：token 既可能是凭据名，也可能是用量字段，
# 只有真正像凭据的键才脱敏
TOKEN_COUNT_KEY_RE = re.compile(
    r"^(total|prompt|completion|cached|input|output)?_?tokens?(_count)?$", re.I)
SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|token|password|secret|cookie|credential"
    r"|phone|id_card|email)", re.I)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._\-]+")
SK_RE = re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")
PHONE_RE = re.compile(r"\b1[3-9]\d{9}\b")
ID_CARD_RE = re.compile(r"\b\d{17}[\dXx]\b")

# 这些字段只在 REDACTED/FULL 里出现内容，METADATA 一律置 null
BODY_FIELDS = ("input", "output", "request_messages", "rendered_messages",
               "input_arguments", "output_result", "raw_response",
               "structured_response", "final_context", "retrieved_documents",
               "reranked_documents", "variables", "events", "llm_calls",
               "tool_calls", "retrievals")


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key)) and not TOKEN_COUNT_KEY_RE.match(key)


def redact(value, mode: str):
    if mode in ("OFF", "METADATA"):
        return None
    if mode == "FULL":
        return value
    if isinstance(value, dict):
        return {k: ("***" if is_sensitive_key(k) else redact(v, mode))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, mode) for v in value]
    if isinstance(value, str):
        return _scrub(value)
    return value


def _scrub(text: str) -> str:
    text = BEARER_RE.sub("Bearer ***", text)
    text = SK_RE.sub("sk-***", text)
    text = PHONE_RE.sub("***", text)
    text = ID_CARD_RE.sub("***", text)
    return text


def redact_record(record: dict, mode: str) -> dict:
    """只在记录内容层面工作：kind/ids/tenant_id/时间戳/计数永远原样保留。"""
    if mode == "FULL":
        return record
    if mode == "METADATA":
        return {k: (None if k in BODY_FIELDS else v) for k, v in record.items()}
    return redact(record, "REDACTED")
```

`capture.resolve_mode`：`OFF` 优先；`degraded=True` 时把 `FULL/REDACTED` 降到 `METADATA`；租户覆盖只在生效模式非 OFF 时起作用。

```python
# backend/app/observability/capture.py
VALID_MODES = ("OFF", "METADATA", "REDACTED", "FULL")


def resolve_mode(configured: str, tenant_override: str | None = None,
                 *, degraded: bool = False) -> str:
    mode = configured if configured in VALID_MODES else "REDACTED"
    if mode == "OFF":
        return "OFF"
    if tenant_override in VALID_MODES and tenant_override != "OFF":
        mode = tenant_override
    if degraded and mode in ("FULL", "REDACTED"):
        return "METADATA"
    return mode
```

Task 5 把 `tracer.emit` 换成脱敏版（Task 1 的版本不 import `capture`，否则 P1 跑不起来）：

```python
# backend/app/observability/tracer.py（Task 5 修改 emit）
    def emit(self, record: dict) -> bool:
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
```

`get_tracer()` 已在 Task 1 传入 `capture_mode=s.obs_capture_mode`；`capture.resolve_mode` 的 `degraded` 取自 `collector.degraded`（连续 5 批写失败后由采集队列置位，写入恢复后自动清除）。

**脱敏不得触碰的字段**：`tenant_id`、`trace_id`、`span_id`、`parent_span_id`、`kind`、`status`、`start_time`/`end_time`/`duration_ms`、各类 `*_tokens` 计数、`total_cost`/`cost_unavailable`。它们既是写入端 NOT NULL/UPSERT 的依据，也是看板聚合的口径；被脱敏改写成 `"***"` 会直接导致写库失败或统计归零。上面 `redact_record` 的实现与 `test_token_counts_ids_and_timestamps_are_never_masked` 共同守住这条。

- [ ] **Step 5: 运行并提交**

Run: `cd backend && pytest tests/unit/observability -v`
Expected: 全部 passed

```bash
git commit -m "feat(obs): 四种采集模式与入队前脱敏"
```

---

## 观测 P2：持久化、Prompt 与调用记录

### Task 6: 批量写入（traces/spans/events/llm_calls/tool_calls）

**Files:**
- Create: `backend/app/observability/writer.py`、`backend/app/observability/models/{llm_call.py, tool_call.py}`、`backend/migrations/versions/0003_obs_llm_tool_prompt.py`（仅 llm_calls/tool_calls 部分，prompt 三张表在 Task 7 追加同 revision）
- Modify: `backend/app/main.py`（lifespan 里装 writer 并启停 collector）
- Test: `backend/tests/integration/observability/test_writer.py`

**Interfaces:**
- Produces: `BatchWriter.write(records: list[dict]) -> None`；`trace.close` 时计算并写入 `total_spans/error_count/total_tokens/total_cost/cost_unavailable_count`
- 写入语义（每条记录都自带 `tenant_id`，写入端不读上下文）：
  - `trace.start` → INSERT `traces`（`ON CONFLICT (trace_id) DO NOTHING`）
  - `span.open` → INSERT `spans`，`status='running'`（`ON CONFLICT (span_id) DO NOTHING`）；只用于 trace 根 span
  - `span.close` → UPSERT `spans`（`ON CONFLICT (span_id) DO UPDATE` 全字段），把 `running` 覆盖成终态；随后按 1:N 追加 `llm_calls`/`tool_calls`/`retrieval_records` 与 `trace_events`
  - `trace.close` → 聚合 UPDATE `traces`（含 `output`），并回写根 span 的 `status`/`end_time`/`duration_ms`
  - **子记录 ID 注入**：`LLMCallRecord`/`ToolCallRecord`/`RetrievalRecord` 自身不带 `span_id`/`trace_id`/`tenant_id`，写入端从所属 `span.close` 记录注入；否则这些 NOT NULL 列无从取值。`trace_events` 的 `tenant_id`/`trace_id`/`span_id` 同理取自所属记录，`sequence_number` 按 trace 内自增。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/observability/test_writer.py
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.collector import EventCollector
from app.observability.records import LLMCallRecord, ToolCallRecord
from app.observability.tracer import Tracer
from app.observability.writer import BatchWriter

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@contextmanager
def tenant(tid=TENANT):
    token = set_tenant_id(tid)
    try:
        yield tid
    finally:
        reset_tenant_id(token)


def make_tracer(writer):
    return Tracer(enabled=True, sample_rate=1.0,
                  collector=EventCollector(maxsize=100, batch_size=100,
                                           flush_ms=100, writer=writer))


@pytest.mark.asyncio
async def test_closed_span_persists_llm_call_and_cost_unavailable(session, session_factory):
    writer = BatchWriter(session_factory)
    t = make_tracer(writer)

    with tenant():
        async with t.start_trace("demo") as trace:
            trace.set_output({"ok": True})
            async with t.start_span("llm.x", "llm") as span:
                span.record_llm_call(LLMCallRecord(provider="mock", model_name="m1",
                                                   total_tokens=10, cost=None))
        trace_id = trace.trace_id
        # 必须在两个 CM 之外取记录，否则拿不到 trace.close，汇总列会是空的
        await writer.write(t.collector.pending())

    row = (await session.execute(
        text("SELECT total_tokens, total_cost, cost_unavailable_count "
             "FROM traces WHERE trace_id=:t"), {"t": trace_id})).one()
    assert row.total_tokens == 10
    assert row.total_cost is None
    assert row.cost_unavailable_count == 1

    output_ok = await session.scalar(text(
        "SELECT jsonb_extract_path_text(output, 'ok') FROM traces WHERE trace_id=:t"),
        {"t": trace_id})
    assert output_ok == "true"

    span_status = await session.scalar(
        text("SELECT status FROM spans WHERE trace_id=:t AND span_type='llm'"), {"t": trace_id})
    assert span_status == "succeeded"


@pytest.mark.asyncio
async def test_tool_call_rows_are_one_to_many_per_span(session, session_factory):
    writer = BatchWriter(session_factory)
    t = make_tracer(writer)

    with tenant():
        async with t.start_trace("demo"):
            async with t.start_span("tool.q", "tool", tool_id="q") as span:
                span.record_tool_call(ToolCallRecord("q", "query", execution_status="failed"))
                span.record_tool_call(ToolCallRecord("q", "query", execution_status="succeeded",
                                                     retry_count=1))
        await writer.write(t.collector.pending())

    count = await session.scalar(text("SELECT count(*) FROM tool_calls WHERE tool_id='q'"))
    assert count == 2
```

- [ ] **Step 2–4: 实现**

`BatchWriter` 在单个事务内按 `kind` 分派：`trace.start` → INSERT（`ON CONFLICT (trace_id) DO NOTHING`）；`span.open` → INSERT（`ON CONFLICT (span_id) DO NOTHING`）；`span.close` → UPSERT span（把 `running` 覆盖成终态）→ 对每个 `llm_calls/tool_calls/retrievals` 追加 INSERT → 对 `events` 追加 `trace_events`（`sequence_number` 用模块级计数器，按 trace 从当前最大值续）；`trace.close` → 聚合 UPDATE + 回写根 span：

```sql
UPDATE traces t SET
  status = :status,
  output = :output,
  end_time = :end_time,
  duration_ms = :duration_ms,
  total_spans = (SELECT count(*) FROM spans s WHERE s.trace_id = t.trace_id),
  error_count = (SELECT count(*) FROM spans s WHERE s.trace_id = t.trace_id
                 AND s.status IN ('failed','timeout')),
  total_tokens = COALESCE((SELECT sum(l.total_tokens) FROM llm_calls l
                 JOIN spans s ON s.span_id = l.span_id WHERE s.trace_id = t.trace_id), 0),
  total_cost = (SELECT sum(l.cost) FROM llm_calls l
                 JOIN spans s ON s.span_id = l.span_id WHERE s.trace_id = t.trace_id),
  cost_unavailable_count = (SELECT count(*) FROM llm_calls l
                 JOIN spans s ON s.span_id = l.span_id
                 WHERE s.trace_id = t.trace_id AND l.cost IS NULL)
WHERE t.trace_id = :trace_id;

-- 根 span 是 span.open 建的行，trace.close 时补终态与耗时
UPDATE spans SET status = :status, end_time = :end_time, duration_ms = :duration_ms
WHERE span_id = :root_span_id AND status = 'running';
```

`total_cost` 在没有任何 llm_call 时也应为 `NULL`（不是 0），UI 才能区分"没有模型调用"与"成本未知"。

- [ ] **Step 5: 接线到应用生命周期**

```python
# backend/app/main.py（lifespan 内追加；其余部分保持平台 Task 1 的实现）
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    from app.db import get_session_factory
    from app.observability import get_tracer, set_writer
    from app.observability.writer import BatchWriter

    set_writer(BatchWriter(get_session_factory()))
    await get_tracer().collector.start()
    try:
        yield
    finally:
        # 先停采集任务（会写完最后一批），再关闭连接池
        await get_tracer().collector.stop()
```

`OBS_ENABLED=false` 时 `get_tracer()` 返回的实例仍然可以 `collector.start()`（队列空转），不会产生写入。

- [ ] **Step 6: 运行并提交**

Run: `cd backend && pytest tests/integration/observability/test_writer.py -v`
Expected: 2 passed

```bash
git commit -m "feat(obs): 批量写入与 trace 汇总口径"
```

---

### Task 7: Prompt 模板、不可变版本与发布/回滚

**Files:**
- Create: `backend/app/observability/models/prompt.py`、`backend/app/observability/prompt/version_manager.py`、`backend/app/observability/api/prompts.py`、`backend/migrations/versions/0003_obs_llm_tool_prompt.py`（prompt 部分：`prompt_templates/prompt_versions/prompt_executions` 三张表）
- Test: `backend/tests/integration/observability/test_prompt_versions.py`

**Interfaces:**
- Produces: `create_template(...)`、`add_version(...)`、`publish(template_id, version)`、`rollback(template_id, version)`、`get_active(template_id) -> (version, row)`；`prompt_executions` 由 Task 8 写入，此处只建表
- 约束：`prompt_versions` 无 UPDATE 路径（只有 INSERT）；`publish/rollback` 只改 `prompt_templates.active_version`；`prompt_executions` 按 spec §5.2 全字段建表（`trace_id/span_id/template_id/template_version/variables/rendered_messages/prompt_hash/render_status/model_name` + `tenant_id/created_at`）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/observability/test_prompt_versions.py
import pytest

from tests.conftest import auth_headers


@pytest.mark.asyncio
async def test_publish_does_not_mutate_existing_version(client, admin_token):
    tpl = (await client.post("/api/v1/observability/prompts", headers=auth_headers(admin_token),
                             json={"template_id": "vision_analysis", "template_name": "视觉分析",
                                   "category": "agent"})).json()["data"]
    v1 = await client.post(f"/api/v1/observability/prompts/{tpl['id']}/versions",
                           headers=auth_headers(admin_token),
                           json={"system_prompt": "S1", "user_prompt_template": "U {{q}}",
                                 "variables_schema": {"type": "object", "required": ["q"],
                                                      "properties": {"q": {"type": "string"}}}})
    assert v1.json()["data"]["version"] == 1

    await client.post(f"/api/v1/observability/prompts/{tpl['id']}/versions",
                      headers=auth_headers(admin_token),
                      json={"system_prompt": "S2", "user_prompt_template": "U2 {{q}}",
                            "variables_schema": {"type": "object"}})
    await client.post(f"/api/v1/observability/prompts/{tpl['id']}/publish",
                     headers=auth_headers(admin_token), json={"version": 2})
    await client.post(f"/api/v1/observability/prompts/{tpl['id']}/rollback",
                     headers=auth_headers(admin_token), json={"version": 1})

    detail = (await client.get(f"/api/v1/observability/prompts/{tpl['id']}",
                               headers=auth_headers(admin_token))).json()["data"]
    assert detail["active_version"] == 1
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][1]["system_prompt"] == "S1"


@pytest.mark.asyncio
async def test_viewer_cannot_publish_prompt(client, viewer_token):
    r = await client.post("/api/v1/observability/prompts/1/publish",
                          headers=auth_headers(viewer_token), json={"version": 1})
    assert r.status_code == 403
```

- [ ] **Step 2–4: 实现**

`prompt_versions` 建唯一约束 `(template_id, version)`；版本号用 `SELECT coalesce(max(version),0)+1`（事务内 `FOR UPDATE` 锁模板行）。`publish/rollback` 校验目标版本存在后 `UPDATE prompt_templates SET active_version = :v`。**不提供** `PATCH /versions/{n}`（返回 405）。

- [ ] **Step 5: 运行并提交**

Run: `make migrate && cd backend && pytest tests/integration/observability/test_prompt_versions.py -v`
Expected: 2 passed

```bash
git commit -m "feat(obs): Prompt 模板与不可变版本、发布/回滚"
```

---

### Task 8: Prompt 渲染、执行记录与 Playground

**Files:**
- Create: `backend/app/observability/prompt/{renderer.py, recorder.py}`、`backend/app/observability/api/prompts.py`（追加 playground）
- Test: `backend/tests/unit/observability/test_prompt_renderer.py`、`backend/tests/integration/observability/test_prompt_execution.py`

**Interfaces:**
- Produces: `render(template_row, variables) -> RenderResult(status, messages, prompt_hash)`；`record_execution(session, trace_id, span_id, template_id, version, variables, messages, status)`
- `record_execution` 的 `tenant_id` 取自 `get_tenant_id()`（与 tracer 同源）；无租户上下文时直接返回、不写库，也不抛异常（观测不影响业务）
- 校验：先 JSON Schema（`type/required/properties/enum`，不引入完整 jsonschema 依赖，用 40 行子集实现），再 Jinja2 `StrictUndefined`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/observability/test_prompt_renderer.py
from types import SimpleNamespace

from app.observability.prompt.renderer import render


def tpl(system="S", user="U {{q}}", schema=None):
    return SimpleNamespace(system_prompt=system, user_prompt_template=user,
                           variables_schema=schema or {"type": "object", "required": ["q"],
                                                       "properties": {"q": {"type": "string"}}})


def test_render_success_and_stable_hash():
    r1 = render(tpl(), {"q": "hi"})
    r2 = render(tpl(), {"q": "hi"})
    assert r1.status == "ok"
    assert [m["role"] for m in r1.messages] == ["system", "user"]
    assert r1.messages[1]["content"] == "U hi"
    assert r1.prompt_hash == r2.prompt_hash


def test_missing_variable_is_schema_error_not_empty_string():
    r = render(tpl(), {})
    assert r.status == "schema_error"
    assert r.messages == []


def test_unexpected_placeholder_is_render_error():
    r = render(tpl(user="U {{other}}"), {"q": "hi"})
    assert r.status == "render_error"


def test_hash_changes_when_variables_change():
    assert render(tpl(), {"q": "a"}).prompt_hash != render(tpl(), {"q": "b"}).prompt_hash
```

```python
# backend/tests/integration/observability/test_prompt_execution.py
import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.prompt.recorder import record_execution
from tests.conftest import TENANT


@pytest.mark.asyncio
async def test_record_execution_persists_rendered_messages(session):
    messages = [{"role": "system", "content": "S"},
                {"role": "user", "content": "U hi"}]
    token = set_tenant_id(TENANT)
    try:
        await record_execution(session, trace_id="f" * 32, span_id="d" * 16,
                               template_id=1, version=3, variables={"q": "hi"},
                               messages=messages, status="ok")
    finally:
        reset_tenant_id(token)

    row = (await session.execute(text(
        "SELECT tenant_id, template_version, render_status, prompt_hash,"
        " rendered_messages->1->>'content' AS user_msg"
        " FROM prompt_executions WHERE trace_id=:t"), {"t": "f" * 32})).one()
    assert row.tenant_id == TENANT
    assert row.template_version == 3
    assert row.render_status == "ok"
    assert len(row.prompt_hash) == 64
    assert row.user_msg == "U hi"
```

- [ ] **Step 2–4: 实现**

```python
# backend/app/observability/prompt/renderer.py
import hashlib
import json
from dataclasses import dataclass, field

from jinja2 import Environment, StrictUndefined

_env = Environment(undefined=StrictUndefined, autoescape=False)


@dataclass
class RenderResult:
    status: str            # ok | schema_error | render_error
    messages: list[dict] = field(default_factory=list)
    prompt_hash: str = ""


def _validate(variables: dict, schema: dict) -> list[str]:
    errors: list[str] = []
    for name in schema.get("required", []):
        if name not in variables:
            errors.append(f"missing required variable: {name}")
    for name, spec in (schema.get("properties") or {}).items():
        if name in variables:
            expected = spec.get("type")
            if expected and not _type_ok(variables[name], expected):
                errors.append(f"{name} expected {expected}")
        if "enum" in spec and name in variables and variables[name] not in spec["enum"]:
            errors.append(f"{name} not in enum")
    return errors


def _type_ok(value, expected: str) -> bool:
    mapping = {"string": str, "integer": int, "number": (int, float),
               "boolean": bool, "object": dict, "array": list}
    py = mapping.get(expected)
    return True if py is None else isinstance(value, py)


def _hash(messages: list[dict]) -> str:
    canonical = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render(template, variables: dict) -> RenderResult:
    schema = template.variables_schema or {"type": "object"}
    if _validate(variables, schema):
        return RenderResult(status="schema_error")
    try:
        messages = [
            {"role": "system", "content": _env.from_string(template.system_prompt or "")
             .render(**variables)},
            {"role": "user", "content": _env.from_string(template.user_prompt_template or "")
             .render(**variables)},
        ]
    except Exception:
        return RenderResult(status="render_error")
    return RenderResult(status="ok", messages=messages, prompt_hash=_hash(messages))
```

`record_execution` 按 `capture_mode`：`FULL/REDACTED` 存 messages（先脱敏）；`METADATA` 只存 hash；`OFF` 只存 template_id/version。Playground：`execute=false` → 返回渲染结果；`execute=true`（operator+）→ 开新 trace（`source="playground"`）调 `LLMClient.chat`，返回响应与 trace_id，**不写** `agent_runs`。

- [ ] **Step 5: 运行并提交**

Run: `cd backend && pytest tests/unit/observability/test_prompt_renderer.py tests/integration/observability/test_prompt_execution.py -v`
Expected: 5 passed（渲染 4 + 执行记录 1）

```bash
git commit -m "feat(obs): Prompt 渲染、执行记录与 Playground"
```

---

## 观测 P3–P7（契约级，进入前展开为代码级）

| Task | 交付物 | 文件 | 关键约束 | 失败测试 |
|---|---|---|---|---|
| 9 | LLM/Tool 调用记录的查询面 | `observability/api/{traces,prompts}.py` 追加 span 详情扩展；`analytics_service.py` | `GET /spans/{id}` 返回 `llm_calls/tool_calls/retrieval_records` 三组数组（1:N）；成本缺失显示 `null` 且带 `unavailable` 标记 | `test_span_detail.py`：一个 span 两次 attempt 返回两行工具调用 |
| 10 | Retrieval 埋点与 `0004` | `0004_obs_retrieval.py`、`services/analytics_service.py` | 平台 `rag/retriever.py` 调 `span.record_retrieval(...)`；记录召回文档与分数、最终 context | `test_retrieval_recorded.py`：召回 5 条 → 表内 5 条文档、分数非空 |
| 11 | LangGraph 回调（不启用 interrupt） | `observability/callback/langgraph_cb.py` | 只认 `metadata.langgraph_node` 建 node span；`on_llm_*`/`on_tool_*` 标记 `instrumented_by=langgraph`，与业务埋点不重复建 span | `test_langgraph_callback.py`：合成 3 节点图 → 1 workflow span + 3 node span；合成 interrupt/resume → 2 个事件 |
| 12 | 跨进程传播 | `observability/propagation.py` | `traceparent` 解析/生成；Celery `__trace__` kwargs 注入与 `task_prerun` 恢复；MQTT 属性透传；线程池 `copy_context` 包装 | `test_propagation.py`：Celery 任务内 `tracer.current_trace_id()` 与调用方一致；线程池同理 |
| 13 | 降级状态机与指标接线 | `observability/capture.py`、`collector.py` | 连续 5 批写失败 → 切 `METADATA` 并发 `degraded` 事件；恢复后回配置模式；`/metrics` 暴露丢弃数 | `test_degrade_state_machine.py`：注入连续失败 → 模式降级、事件只发一次 |
| 14 | 观测前端壳 + Overview | `web/src/{api,stores}/observability.ts`、`views/observability/Overview.vue`、`components/observability/MetricCard.vue` | 10 个指标卡 + 8 类图表；每卡 tooltip 显示口径；成本卡显示"覆盖 X/Y 模型" | `test_overview.spec.ts`：缺价模型时显示覆盖比例而非总额 |
| 15 | Trace Explorer 列表 | `views/observability/Traces.vue`、`components/observability/FilterBar.vue` | 过滤走 query 参数，禁止前端全量；虚拟滚动；URL 承载筛选 | `test_traces_filter.spec.ts`：改筛选 → 请求参数正确、刷新后保留 |
| 16 | Trace Detail 三栏 | `views/observability/TraceDetail.vue`、`components/observability/{SpanTree,SpanTimeline,SpanInspector,JsonViewer}.vue` | 左树中时间线右 Inspector；10 个 Tab 懒加载；选中联动；失败节点默认展开 | `test_trace_detail.spec.ts`：点第 3 个 span → Inspector 切换且 Timeline 高亮 |
| 17 | 三个分析页 + Prompt UI | `views/observability/{Tools,Models,Agents,Prompts,Playground,Settings}.vue` | 全部数据来自 analytics API；Prompt 对比用 diff 视图；Settings 改采集模式需二次确认 | `test_prompt_compare.spec.ts`：两版本渲染差异高亮 |

### Task 13 代码级展开（已实现）

- **文件**：`observability/metrics.py` 新增指标；`observability/collector.py` 的 `EventCollector` 接入边沿触发状态机；`capture.py` 的 `resolve_mode` 语义不变（`OFF` 优先，降级时 `FULL/REDACTED → METADATA`）。
- **新增指标**：`observability_collector_degraded`（Gauge，1=降级/0=正常）与 `observability_degraded_transitions_total{state}`（Counter，`state ∈ degraded|recovered`）。
- **边沿触发语义**：连续 5 批写失败首次进入降级时置 `degraded=True`、gauge=1、`{state="degraded"}` +1、记录一条 `WARNING`（带失败批次streak）；已在降级态继续失败只累加 `dropped["write_failed"]` 与 `event_dropped{reason="write_failed"}`，不再重复计数。任一批写入成功即恢复：`degraded=False`、gauge=0、`{state="recovered"}` +1、记录一条 `INFO`。
- **`/metrics`**：`app/core/metrics.py#render()` 无需改动，即可输出新 gauge/counter 及既有的 `observability_event_dropped_total{reason="write_failed"}`。
- **测试** `tests/unit/observability/test_degrade_state_machine.py`：`test_degrade_transition_is_emitted_once`（5+2 批失败，事件只发一次、丢弃计数==7）、`test_recovery_returns_to_configured_mode`（恢复事件一次、gauge 归零、`resolve_mode` 语义不变）、`test_dropped_metric_is_exposed`（`render()` 文本含丢弃/降级指标）。断言均按全局注册表的**增量**取值。

### Task 9 代码级展开（已实现）

`GET /api/v1/observability/spans/{span_id}`（viewer 可读，租户取自 `TenantContext`）返回 span 字段 + 三组 1:N 数组：`llm_calls`、`tool_calls`、`retrieval_records`。实现位于 `services/trace_service.py` 的 `get_span` / `_related_records`：

- 先用 `information_schema.columns` 探测 `_RELATED_TABLES` 白名单中的表；表不存在或缺 `tenant_id/span_id` 时该组返回 `[]`，**从不报错**（`0004` 未应用时 `retrieval_records == []`）。
- 表可用时按 `tenant_id = 当前租户 AND span_id = :span_id ORDER BY id` 查询，因此 `tool_calls` 保持插入顺序，attempt 依次为 1、2。输出键与查询表名统一为 `retrieval_records`（此前误写为 `retrievals`）。
- 仅对 `llm_calls` 追加派生布尔 `cost_available = (cost is not None)`；`cost` 原样保留（缺失即 `null`），**不做任何估算**。

测试 `tests/integration/observability/test_span_detail.py`：两次 attempt 的 tool/llm 调用按序返回且 `cost_available` 正确、`retrieval_records` 为空；跨租户 404；无子记录的 span 三组均为空；未知 span 404。

### Task 10 代码级展开（已实现）

- **迁移**：`migrations/versions/0004_obs_retrieval.py`，`revision="0004_obs_retrieval"`、`down_revision="0003_obs_llm_tool_prompt"`。upgrade 先 `CREATE EXTENSION IF NOT EXISTS pg_trgm`，为 `knowledge_chunks.content` 建 `ix_knowledge_chunks_content_trgm`（GIN `gin_trgm_ops`），再建 `retrieval_records`；downgrade 只删表和 trigram 索引，不 drop 扩展。
- **表结构**：`id BigInteger Identity` 主键、`tenant_id UUID`、`span_id String(16)`、`query Text NOT NULL`、`rewritten_query/retrieval_strategy/embedding_model/vector_store/reranker_model/top_k/similarity_threshold/final_context/duration_ms/error`，`retrieved_documents/reranked_documents JSONB NOT NULL DEFAULT '[]'::jsonb`，`created_at timestamptz NOT NULL DEFAULT now()`。**无 `updated_at`、无 attempt 列、无跨表外键**；索引为 `tenant_id`、`span_id`、`(tenant_id, created_at DESC)`。
- **ORM 对齐**：`app/observability/models/retrieval.py` 定义同名模型并在 `models/__init__.py` 注册；`writer.py` 的 `_RETRIEVAL_TABLE` 直接使用 ORM `RetrievalRecord.__table__`（而非无类型 `sa.table`），保证 asyncpg 正确编码 JSONB。写入前仍用 `to_regclass('public.retrieval_records')` 探测，表缺失时跳过检索记录而不让整批失败。
- **埋点**：`app/rag/retriever.py` 在检索开始时开 `retrieval.hybrid` span（`span_type="retrieval"`），设置 `input={query,top_k}`；结束时 `span.record_retrieval(RetrievalRecord(...))` 并 `span.finish("succeeded")`。记录内容来自本次真实执行：`query/strategy/embedding_model/vector_store/reranker_model/top_k/retrieved_documents`（各腿原始分 + rrf/rerank）、`reranked_documents`（最终结果）、`final_context`、`duration_ms`；异常路径记录 `error={"code":"retrieval_error","message":<异常类型>}` 后原样抛出。
- **降级**：无活动 trace 时 tracer 返回 NullSpan，`record_retrieval` 为空操作，检索照常返回；`OBS_ENABLED=false` 同理不落库。没有 embedding/reranker 时字段写 `null` 并在业务响应中标记 `vector_unavailable/reranker_unavailable`，**绝不写入伪造向量或重排分数**。
- **故障隔离**：检索对观测故障完全隔离。tracer 初始化与 `start_span` 失败回退 `NullSpan`；`set_input/set_output/set_error/record_retrieval/finish` 均经 `_obs_safe` 包裹，异常只记 WARNING，不进入业务控制流、不替换业务异常。向量腿额外用 `math.isfinite` 过滤非有限分数，避免污染结果。
- **测试**：`tests/integration/observability/test_retrieval_recorded.py` 4 条——真实 Tracer/EventCollector/BatchWriter + 租户上下文下检索 6 段长文本（≥5 块），断言 `retrieval_records` 恰 1 行且 `query/span_id` 正确、`retrieved_documents` ≥5 条且每条 `score` 非空、`reranked_documents/final_context` 已写、存在 `retrieval.hybrid` span；无 trace 与 OBS 关闭两条路径均检索成功且不新增记录；另有 `ExplodingTracer`/`ExplodingSpan` 故障注入（span 创建失败、span 方法全抛异常）仍返回正常结果，证明埋点异常不外溢。新增 4 条通过，全量 **224 passed**，`alembic check` 为 `No new upgrade operations detected.`。

**P3–P7 退出验收**：`make e2e` 跑 `tests/e2e/test_observability_flow.py`——真实工作流请求后：（1）`/traces` 出现该 trace；（2）`/tree` 有 ≥1 workflow + 6 agent + ≥2 tool + ≥1 llm 节点；（3）Inspector 的 Prompt Tab 显示模板版本与渲染消息；（4）SSE 在运行中推送 ≥3 个事件；（5）`OBS_ENABLED=false` 时同一请求仍成功且 `/traces` 无新增。

---

## 观测 P8：Evaluation 与 Replay（边界）

| 范围 | 本期交付 | 依赖 |
|---|---|---|
| 历史只读 Replay | `GET /traces/{id}/replay` 返回保存的输入/输出/事件序列（P7 已含，不重新执行） | 观测 P1–P7 |
| Dry-run 与 Compare | 需独立 spec：数据集、确定性 evaluator、LLM judge、沙箱重执行、副作用工具 dry-run（消费平台 `tools.dry_run_supported`）、两次执行对比 | 平台 P2（`dry_run_supported`）+ 观测 P1–P7 |
| `0005_evaluation` | `evaluation_records` 表，随 P8 spec 交付 | — |

**不进入本期验收**：实验管理 UI、多人评测标注、LLM judge 校准。

---

## 需求 18 项测试的落点

| # | 用例 | 落点（Task） | 验收形式 |
|---|---|---|---|
| 1 | 正常 Agent 执行 | 平台 12 + 本计划 6 | 集成：span 树有唯一根、agent span 挂在 trace 下 |
| 2 | 多 Agent 并行 | 平台 P3（Task 13/14）+ 本计划 6 | 集成：并行 span 时间区间重叠、父节点唯一 |
| 3 | LLM 超时 | 平台 10（timeout）+ 本计划 6 | 集成：llm span `timeout`、attempt 递增、agent 降级 |
| 4 | Tool 失败 | 平台 9 + 本计划 6 | 集成：tool span `failed` 且 `error.code` 非空 |
| 5 | Tool 重试 | 平台 9 + 本计划 6 | 集成：一个 span 两行 `tool_calls`，`retry_count=1` |
| 6 | Agent 异常 | 平台 12 + 本计划 6 | 集成：agent span `failed`，trace 仍可查 |
| 7 | LangGraph Interrupt | 本计划 11 | 合成 graph：`interrupt` 事件落 `trace_events` |
| 8 | LangGraph Resume | 本计划 11 | 合成 graph：resume 后 node span 挂在同一 workflow span |
| 9 | Prompt 版本切换 | 本计划 7/8 | 集成：发布 v2 后旧 run 的 `template_version` 仍为 1 |
| 10 | RAG 检索记录 | 本计划 10 | 集成：`retrieval_records` 有 query/分数/最终 context |
| 11 | Trace 查询 | 本计划 3/9 + 平台 4 | API：过滤、分页、排序、跨租户 404 |
| 12 | Span 查询 | 本计划 9 | API：tree/children/单条均返回扩展表 |
| 13 | Token 汇总 | 本计划 6 | 单元：`traces.total_tokens == sum(llm_calls.total_tokens)` |
| 14 | 成本统计 | 本计划 6/14 | 单元：缺价 → null + `cost_unavailable_count`；有价 → 精确值 |
| 15 | 监控写入故障 | 本计划 1/5 | 单元 + 集成：队列满/写入失败只计数，业务请求成功 |
| 16 | 数据脱敏 | 本计划 5 | 单元：嵌套 jsonb + 正则；队列内无明文 |
| 17 | 权限控制 | 本计划 7 + 平台 4 | API：viewer 不可 publish；raw 内容需 `observability:raw` 且写审计 |
| 18 | Replay Dry Run | P8（本期不交付） | 集成：副作用工具走 dry_run，不落真实设备指令 |

---

## Self-Review（已执行）

- **Spec 覆盖**：§2 数据流 → Task 1/5/6/13；§2.3 传播 → Task 12；§3 模型与状态 → Task 1/2；§4 五处埋点 → 平台 Task 9/10/12 + 本计划 Task 9/10/11；§5 表与索引 → Task 2/6/7/10；§6 管道 → Task 1/4/5/6/13；§7 Prompt → Task 7/8/17；§8 口径 → Task 14/17 + `analytics_service`；§9 API → Task 3/4/7/9/10/17；§10 前端 → Task 14–17；§11 安全与保留 → Task 5/13 + P7 保留脚本（平台 Task 27）；§12 阶段 → 本文件 Phase 划分；§13 测试 18 项 → 见上一节逐条落点（第 18 项 P8）。
- **占位符扫描**：无 TBD/TODO；P3–P7 的"进入前展开"是 Gate 规则；P8 明确不在本期范围。
- **接口一致性**：`Tracer.start_trace/start_span`、`SpanHandle.record_*`、`LLMCallRecord/ToolCallRecord/RetrievalRecord` 字段、`build_tree` 返回结构（`children` + `orphan`）在平台计划与本计划中同名同义；`down_revision` 链 `0001_core → 0002 → 0003 → 0004 → 0005` 无分叉。
