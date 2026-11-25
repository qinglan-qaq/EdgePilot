"""5s 心跳：cpu/mem/disk/gpu/task_count，缺失值一律 None，不伪造测量。"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .outbox import to_utc_iso, utcnow
from .topics import heartbeat_topic

logger = logging.getLogger(__name__)

MetricsFn = Callable[[], dict]


def _load_psutil() -> Any | None:
    """仅在 psutil 真正可提供测量 API 时返回模块，否则 None。"""
    try:
        import psutil  # noqa: PLC0415 - 允许部署环境缺失该可选依赖
    except Exception:  # noqa: BLE001 - 任何导入异常都视为不可用
        return None
    required = ("cpu_percent", "virtual_memory", "disk_usage")
    if not all(hasattr(psutil, name) for name in required):
        return None
    return psutil


def _safe_percent(read: Callable[[], float]) -> float | None:
    try:
        return round(float(read()), 1)
    except Exception:  # noqa: BLE001 - 单指标失败只降级该指标
        return None


def collect_metrics() -> dict[str, float | None]:
    """真实采集 cpu/mem/disk；psutil 不可用或读取失败时返回 None。

    gpu 当前没有真实探针，恒为 None（绝不编造 GPU 使用率）。
    """
    psutil = _load_psutil()
    if psutil is None:
        return {"cpu": None, "mem": None, "disk": None, "gpu": None}

    return {
        "cpu": _safe_percent(lambda: psutil.cpu_percent(interval=None)),
        "mem": _safe_percent(lambda: psutil.virtual_memory().percent),
        "disk": _safe_percent(lambda: psutil.disk_usage(str(Path.cwd())).percent),
        "gpu": None,
    }


class HeartbeatPublisher:
    def __init__(
        self,
        mqtt: Any,
        node_key: str,
        *,
        interval_s: float = 5.0,
        metrics_fn: MetricsFn = collect_metrics,
        task_count_fn: Callable[[], int] = lambda: 0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("interval_s 必须为正数")
        self.mqtt = mqtt
        self.node_key = node_key
        self.interval_s = float(interval_s)
        self.metrics_fn = metrics_fn
        self.task_count_fn = task_count_fn
        self._clock = clock or utcnow

    def build_payload(self) -> dict:
        metrics = self.metrics_fn() or {}
        return {
            "msg_id": uuid.uuid4().hex,
            "sent_at": to_utc_iso(self._clock()),
            "node_key": self.node_key,
            "cpu": metrics.get("cpu"),
            "mem": metrics.get("mem"),
            "disk": metrics.get("disk"),
            "gpu": metrics.get("gpu"),
            "task_count": self.task_count_fn(),
        }

    def tick(self) -> bool:
        """发布一次心跳：QoS 0、不 retain；离线直接丢弃，不落 outbox。"""
        return self.mqtt.publish(
            heartbeat_topic(self.node_key),
            self.build_payload(),
            qos=0,
            retain=False,
            persist_on_failure=False,
        )

    def run(self, stop_event: Any) -> None:
        """每隔 ``interval_s`` 发一次心跳，直到 stop_event 置位。"""
        while not stop_event.is_set():
            self.tick()
            wait = getattr(stop_event, "wait", None)
            if wait is not None:
                if wait(self.interval_s):
                    break
            else:  # pragma: no cover - 兼容无 wait 的极简 event
                time.sleep(self.interval_s)
