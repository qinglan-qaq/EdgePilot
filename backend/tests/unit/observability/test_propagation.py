"""跨进程传播单元测试：traceparent、Celery、线程池、MQTT 载荷。

全部离线执行：Celery 用 eager ``apply``，MQTT 用 fake client，不需要 broker。
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.config import get_settings
from app.core.tenancy import (
    current_tenant_id_or_none,
    reset_tenant_id,
    set_tenant_id,
)
from app.messaging.mqtt.client import MqttService
from app.observability import context as obs_context
from app.observability.collector import EventCollector
from app.observability.propagation import (
    TRACE_KWARG,
    RemoteSpan,
    RemoteTrace,
    TraceParent,
    bind_context,
    current_traceparent,
    format_traceparent,
    parse_traceparent,
    submit_with_context,
    trace_payload_from_current,
)
from app.observability.tracer import Tracer
from app.tasks import workflow_tasks
from app.worker import celery_app, run_in_worker_loop

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"


class _NullWriter:
    async def write(self, records) -> None:  # noqa: ANN001 - 测试桩
        return None


def _make_tracer(enabled: bool = True) -> Tracer:
    return Tracer(
        enabled=enabled,
        sample_rate=1.0,
        collector=EventCollector(
            maxsize=100, batch_size=100, flush_ms=1000, writer=_NullWriter()
        ),
    )


@pytest.fixture(autouse=True)
def _clean_context():
    obs_context.current_trace.set(None)
    obs_context.current_span.set(None)
    obs_context.sequence.set(0)
    yield
    obs_context.current_trace.set(None)
    obs_context.current_span.set(None)
    obs_context.sequence.set(0)


# ----------------------------------------------------------------- traceparent


def test_traceparent_roundtrip():
    value = format_traceparent(TRACE_ID, SPAN_ID)
    assert value == f"00-{TRACE_ID}-{SPAN_ID}-01"

    parsed = parse_traceparent(value)
    assert parsed == TraceParent(trace_id=TRACE_ID, span_id=SPAN_ID, flags="01")
    assert parsed.version == "00"

    # flags 允许 00；int 形式补齐两位 hex
    assert parse_traceparent(format_traceparent(TRACE_ID, SPAN_ID, 0x0A)).flags == "0a"
    assert parse_traceparent(format_traceparent(TRACE_ID, SPAN_ID, "00")).flags == "00"


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "not-a-traceparent",
        f"00-{TRACE_ID}-{SPAN_ID}",  # 缺 flags
        f"00-{TRACE_ID}-{SPAN_ID}-01-extra",  # 多余后缀
        f"00-{TRACE_ID[:-1]}-{SPAN_ID}-01",  # trace_id 长度错误
        f"00-{TRACE_ID}-{SPAN_ID[:-1]}-01",  # span_id 长度错误
        f"01-{TRACE_ID}-{SPAN_ID}-01",  # 不支持的版本
        f"ff-{TRACE_ID}-{SPAN_ID}-01",  # 非法版本
        f"00-{TRACE_ID.upper()}-{SPAN_ID}-01",  # 必须小写 hex
        f"00-{'z' * 32}-{SPAN_ID}-01",  # 非 hex
        f"00-{'0' * 32}-{SPAN_ID}-01",  # trace_id 全零
        f"00-{TRACE_ID}-{'0' * 16}-01",  # span_id 全零
        f"00-{TRACE_ID}-{SPAN_ID}-0",  # flags 长度错误
        f"00-{TRACE_ID}-{SPAN_ID}-zz",  # flags 非 hex
    ],
)
def test_parse_traceparent_rejects_invalid(value):
    assert parse_traceparent(value) is None


def test_format_traceparent_rejects_zero_and_malformed_ids():
    with pytest.raises(ValueError):
        format_traceparent("0" * 32, SPAN_ID)
    with pytest.raises(ValueError):
        format_traceparent(TRACE_ID, "0" * 16)
    with pytest.raises(ValueError):
        format_traceparent("short", SPAN_ID)
    with pytest.raises(ValueError):
        format_traceparent(TRACE_ID, SPAN_ID, "zzz")


def test_current_traceparent_requires_active_context():
    """没有活动 trace 时只能返回 None，绝不新生成 id。"""
    assert current_traceparent() is None
    assert trace_payload_from_current() is None


# -------------------------------------------------------------------- Celery


def test_celery_task_restores_caller_trace(monkeypatch):
    caller = _make_tracer()
    seen: dict = {}

    def fake_run(factory, timeout=None):  # noqa: ANN001 - 替换事件循环入口
        seen["trace_id"] = caller.current_trace_id()
        seen["tenant"] = current_tenant_id_or_none()
        seen["trace"] = obs_context.current_trace.get()
        seen["span"] = obs_context.current_span.get()
        # 在恢复出的远端父 span 下新建子 span，验证父子挂接正确
        child = caller.start_span("caller.child", "agent")
        seen["child_trace_id"] = child.trace_id
        seen["child_parent_id"] = child.parent_span_id
        child.finish()
        return {"run_id": 1, "status": "succeeded"}

    monkeypatch.setattr(workflow_tasks, "run_in_worker_loop", fake_run)

    token = set_tenant_id(TENANT)
    trace = None
    span = None
    try:
        trace = caller.start_trace("caller")
        span = caller.start_span("caller.work", "agent")
        payload = trace_payload_from_current()
        assert payload is not None
        assert payload["trace_id"] == trace.trace_id
        assert payload["parent_span_id"] == span.span_id
        assert payload["tenant_id"] == str(TENANT)
        assert payload["root_span_id"] == trace.root_span_id

        result = workflow_tasks.execute_run.apply(
            args=[1], kwargs={TRACE_KWARG: payload}
        )
        assert result.successful()
        assert result.get()["status"] == "succeeded"

        # 任务体内看到的是恢复出来的远端 trace / 父 span / 租户
        assert seen["trace_id"] == trace.trace_id
        assert seen["tenant"] == TENANT
        assert isinstance(seen["trace"], RemoteTrace)
        assert seen["trace"] is not trace
        assert isinstance(seen["span"], RemoteSpan)
        assert seen["span"].span_id == span.span_id
        assert seen["child_trace_id"] == trace.trace_id
        assert seen["child_parent_id"] == span.span_id

        # task_postrun 之后调用方上下文必须原样恢复
        assert obs_context.current_trace.get() is trace
        assert obs_context.current_span.get() is span
        assert current_tenant_id_or_none() == TENANT
    finally:
        if span is not None:
            span.finish()
        if trace is not None:
            trace.close()
        reset_tenant_id(token)


def test_celery_task_without_trace_still_runs(monkeypatch):
    monkeypatch.setattr(
        workflow_tasks,
        "run_in_worker_loop",
        lambda factory, timeout=None: {"run_id": 2, "status": "succeeded"},
    )

    result = workflow_tasks.execute_run.apply(args=[2])

    assert result.successful()
    assert result.get()["status"] == "succeeded"
    assert obs_context.current_trace.get() is None


def test_enqueue_injects_private_trace_kwarg(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(get_settings(), "celery_enabled", True)

    tracer = _make_tracer()
    token = set_tenant_id(TENANT)
    trace = None
    try:
        trace = tracer.start_trace("caller")
        assert workflow_tasks.enqueue_workflow_run(9, "review") is True
    finally:
        if trace is not None:
            trace.close()
        reset_tenant_id(token)

    assert len(calls) == 1
    _, send_kwargs = calls[0]
    payload = send_kwargs["kwargs"][TRACE_KWARG]
    assert payload["trace_id"] == trace.trace_id
    assert payload["tenant_id"] == str(TENANT)
    assert "traceparent" not in payload  # 只传最小上下文，不放 header/密钥


def test_enqueue_without_trace_sends_no_private_kwarg(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        celery_app, "send_task", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(get_settings(), "celery_enabled", True)

    assert workflow_tasks.enqueue_workflow_run(10) is True

    assert len(calls) == 1
    _, send_kwargs = calls[0]
    assert "kwargs" not in send_kwargs


# ------------------------------------------------------ 常驻循环 / 线程池


def test_run_in_worker_loop_propagates_caller_context():
    """Celery 恢复的 trace 必须跨进常驻事件循环，_execute_run 才能看到。"""
    tracer = _make_tracer()
    token = set_tenant_id(TENANT)
    trace = None
    span = None
    try:
        trace = tracer.start_trace("worker-loop")
        span = tracer.start_span("worker-loop.work", "agent")
        sequence = obs_context.sequence.get()

        async def body():
            current_span = obs_context.current_span.get()
            return (
                tracer.current_trace_id(),
                getattr(current_span, "span_id", None),
                current_tenant_id_or_none(),
                obs_context.sequence.get(),
            )

        seen = run_in_worker_loop(body, timeout=5)

        # 跨线程设置发生在 loop Task 的 context 副本里，调用方上下文不变
        assert tracer.current_trace_id() == trace.trace_id
        assert obs_context.current_span.get() is span
        assert current_tenant_id_or_none() == TENANT
    finally:
        if span is not None:
            span.finish()
        if trace is not None:
            trace.close()
        reset_tenant_id(token)

    assert seen[0] == trace.trace_id
    assert seen[1] == span.span_id
    assert seen[2] == TENANT
    assert seen[3] == sequence


def test_run_in_worker_loop_without_context_still_runs():
    async def body():
        return (
            obs_context.current_trace.get(),
            obs_context.current_span.get(),
            current_tenant_id_or_none(),
            obs_context.sequence.get(),
        )

    trace, span, tenant, sequence = run_in_worker_loop(body, timeout=5)

    assert trace is None
    assert span is None
    assert tenant is None
    assert sequence == 0


def test_threadpool_worker_sees_caller_trace():
    tracer = _make_tracer()
    token = set_tenant_id(TENANT)
    trace = None
    span = None
    try:
        trace = tracer.start_trace("pool")
        span = tracer.start_span("pool.work", "agent")

        def worker():
            return (
                tracer.current_trace_id(),
                current_tenant_id_or_none(),
                obs_context.current_span.get(),
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            # 裸 submit 使用线程自己的空 context（对照组）
            assert pool.submit(worker).result()[0] is None
            # 调用线程复制 context 后，worker 看到同一 trace / span / 租户
            seen = submit_with_context(pool, worker).result()
            assert seen[0] == trace.trace_id
            assert seen[1] == TENANT
            assert seen[2] is span

            # bind_context 同样在调用线程捕获 context
            bound = bind_context(worker)
            seen_bound = pool.submit(bound).result()
            assert seen_bound[0] == trace.trace_id
    finally:
        if span is not None:
            span.finish()
        if trace is not None:
            trace.close()
        reset_tenant_id(token)


# ---------------------------------------------------------------------- MQTT


class _PublishInfo:
    rc = 0


class _FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, topic, payload, qos=0, retain=False):  # noqa: ANN001 - paho 签名
        self.published.append(
            {
                "topic": topic,
                "payload": json.loads(payload),
                "qos": qos,
                "retain": retain,
            }
        )
        return _PublishInfo()


class _FakeDedupe:
    async def close(self) -> None:
        return None


def _make_mqtt_service():
    client = _FakeMqttClient()
    service = MqttService(
        settings=get_settings(),
        session_factory=lambda: None,
        dedupe=_FakeDedupe(),
        tenant_id=TENANT,
    )
    service._client = client
    service._connected = True
    return service, client


async def test_mqtt_publish_includes_traceparent_when_trace_exists():
    tracer = _make_tracer()
    token = set_tenant_id(TENANT)
    trace = None
    span = None
    try:
        trace = tracer.start_trace("mqtt")
        span = tracer.start_span("mqtt.publish", "tool")
        service, client = _make_mqtt_service()

        assert await service.publish_command(
            "node-1", "start_task", {"foo": "bar"}, qos=1
        )
    finally:
        if span is not None:
            span.finish()
        if trace is not None:
            trace.close()
        reset_tenant_id(token)

    assert len(client.published) == 1
    message = client.published[0]
    assert message["topic"] == "edge/node-1/cmd/start_task"
    assert message["qos"] == 1
    assert message["payload"]["foo"] == "bar"
    assert message["payload"]["msg_id"]
    assert message["payload"]["sent_at"]
    assert message["payload"]["traceparent"] == (
        f"00-{trace.trace_id}-{span.span_id}-01"
    )


async def test_mqtt_publish_without_trace_is_still_publishable():
    service, client = _make_mqtt_service()

    assert await service.publish_command("node-1", "sync_config", {"a": 1})

    assert len(client.published) == 1
    message = client.published[0]
    assert message["topic"] == "edge/node-1/cmd/sync_config"
    assert message["payload"]["a"] == 1
    assert "traceparent" not in message["payload"]


async def test_mqtt_publish_keeps_explicit_traceparent():
    explicit = f"00-{'a' * 32}-{'b' * 16}-01"
    service, client = _make_mqtt_service()

    assert await service.publish_command(
        "node-1", "probe_device", {"traceparent": explicit}
    )

    assert client.published[0]["payload"]["traceparent"] == explicit
