"""采集降级状态机的边沿触发与指标接线测试。

Prometheus 注册表是进程级全局对象，因此所有断言都取**增量**（delta），
避免与同进程内其它测试的计数互相干扰。
"""

import pytest
from prometheus_client import REGISTRY

from app.observability.capture import resolve_mode
from app.observability.collector import EventCollector

DEGRADED_GAUGE = "observability_collector_degraded"
TRANSITIONS = "observability_degraded_transitions_total"


class _AlwaysFailWriter:
    """写入始终失败的 writer，用于触发降级。"""

    async def write(self, records) -> None:
        raise RuntimeError("pg down")


class _FlakyWriter:
    """可切换成功/失败的 writer，用于验证恢复。"""

    def __init__(self) -> None:
        self.fail = True

    async def write(self, records) -> None:
        if self.fail:
            raise RuntimeError("pg down")


def _gauge() -> float | None:
    """读取降级 gauge 的当前值。"""
    return REGISTRY.get_sample_value(DEGRADED_GAUGE)


def _transitions(state: str) -> float:
    """读取指定状态切换计数的当前值（缺失按 0 处理）。"""
    value = REGISTRY.get_sample_value(TRANSITIONS, {"state": state})
    return 0.0 if value is None else value


@pytest.mark.asyncio
async def test_degrade_transition_is_emitted_once():
    writer = _AlwaysFailWriter()
    c = EventCollector(maxsize=10, batch_size=1, flush_ms=10, writer=writer,
                       retry_delays=(0, 0))
    degraded_before = _transitions("degraded")

    for _ in range(5):
        await c._write_with_retry([{"i": 0}])

    assert c.degraded is True
    assert _gauge() == 1.0
    assert _transitions("degraded") - degraded_before == 1

    # 已在降级态时继续失败：丢弃计数继续累加，但切换事件不重复发。
    for _ in range(2):
        await c._write_with_retry([{"i": 1}])

    assert c.degraded is True
    assert _transitions("degraded") - degraded_before == 1
    assert c.dropped["write_failed"] == 7


@pytest.mark.asyncio
async def test_recovery_returns_to_configured_mode():
    writer = _FlakyWriter()
    c = EventCollector(maxsize=10, batch_size=1, flush_ms=10, writer=writer,
                       retry_delays=(0, 0))

    for _ in range(5):
        await c._write_with_retry([{"i": 0}])
    assert c.degraded is True

    recovered_before = _transitions("recovered")
    writer.fail = False
    await c._write_with_retry([{"i": 1}])

    assert c.degraded is False
    assert _gauge() == 0.0
    assert _transitions("recovered") - recovered_before == 1

    # 恢复后回到配置模式；降级态下仍降到 METADATA（语义未变）。
    assert resolve_mode("FULL", degraded=False) == "FULL"
    assert resolve_mode("FULL", degraded=True) == "METADATA"


@pytest.mark.asyncio
async def test_dropped_metric_is_exposed():
    from app.core.metrics import render

    # 至少产生一次 write_failed 丢弃，保证带标签的计数器已出现在注册表中。
    c = EventCollector(maxsize=10, batch_size=1, flush_ms=10,
                       writer=_AlwaysFailWriter(), retry_delays=(0, 0))
    await c._write_with_retry([{"i": 0}])

    body, _content_type = render()
    text = body.decode("utf-8")
    assert "observability_event_dropped_total" in text
    assert DEGRADED_GAUGE in text
