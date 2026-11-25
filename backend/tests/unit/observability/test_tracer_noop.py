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
