"""事件端到端延迟基准（P7 Task 28）。

口径：本机 MQTT publish → 控制面 ``vision_events.received_at`` 落库。
不含浏览器 SSE 渲染时间；SSE 只读已落库数据，属于展示层附加延迟。

从仓库根目录运行（需要 Docker 栈的 MQTT 与 PostgreSQL 已启动）::

    python scripts/bench_event_latency.py --count 100 --rate 20

脚本使用独立 ``bench-node-<随机>`` 节点和唯一 ``dedup_key``，默认结束后
删除本次基准写入的事件与节点，不污染演示数据；``--keep`` 可保留现场。

安全约定：

- 事件载荷显式标记 ``simulated=True``，不伪造真实检测结论；
- 只写 ``vision_events``，不触发 Agent/工作流；
- 缺少依赖或连接失败以非零码退出，不输出伪造的成功结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from datetime import datetime, timezone
from typing import Any

DEFAULT_DB = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://edgepilot:edgepilot@127.0.0.1:5433/edgepilot",
)
DEFAULT_TENANT = os.environ.get(
    "EDGEPILOT_TENANT_ID", "00000000-0000-0000-0000-000000000001"
)


def _dsn(url: str) -> str:
    """asyncpg 只认 postgres/postgresql，不认 SQLAlchemy 的 ``+asyncpg``。"""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
        "postgres+asyncpg://", "postgres://", 1
    )


def _percentile(values: list[float], fraction: float) -> float:
    """最近秩百分位；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _wait_node(conn, tenant_id: uuid.UUID, node_key: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = await conn.fetchval(
            "SELECT 1 FROM edge_nodes WHERE tenant_id = $1 AND node_key = $2",
            tenant_id,
            node_key,
        )
        if found:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError(f"节点注册超时：{node_key}")


async def _collect(
    conn,
    tenant_id: uuid.UUID,
    sent: dict[str, float],
    timeout_s: float,
) -> list[tuple[str, float]]:
    """轮询到所有 dedup_key 都落库；返回 (key, 毫秒延迟)。"""
    keys = list(sent)
    deadline = time.monotonic() + timeout_s
    found: dict[str, float] = {}
    while time.monotonic() < deadline:
        rows = await conn.fetch(
            "SELECT dedup_key, received_at FROM vision_events "
            "WHERE tenant_id = $1 AND dedup_key = ANY($2::text[])",
            tenant_id,
            keys,
        )
        for row in rows:
            found[row["dedup_key"]] = row["received_at"].timestamp() * 1000.0
        if len(found) == len(keys):
            break
        await asyncio.sleep(0.2)

    missing = [key for key in keys if key not in found]
    if missing:
        raise RuntimeError(f"{len(missing)} 条事件在 {timeout_s}s 内未落库")
    return [(key, found[key] - sent[key] * 1000.0) for key in keys]


async def run(args: argparse.Namespace) -> int:
    import asyncpg
    import paho.mqtt.client as mqtt

    tag = uuid.uuid4().hex[:8]
    node_key = f"bench-node-{tag}"
    tenant_id = uuid.UUID(args.tenant_id)
    topic = f"edge/{node_key}/event"

    conn = await asyncpg.connect(_dsn(args.database_url))
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-{tag}"
    )
    try:
        client.connect(args.mqtt_host, args.mqtt_port, keepalive=30)
        client.loop_start()

        register = {
            "msg_id": f"bench-register-{tag}",
            "sent_at": _iso_now(),
            "node_key": node_key,
            "name": f"基准节点 {tag}",
            "os": "bench",
            "arch": "x86_64",
            "agent_version": "bench",
        }
        client.publish(f"edge/{node_key}/register", json.dumps(register), qos=1)
        await _wait_node(conn, tenant_id, node_key, args.timeout)

        sent: dict[str, float] = {}
        interval = 1.0 / args.rate if args.rate > 0 else 0.0
        start = time.monotonic()
        for index in range(args.count):
            msg_id = f"bench-{tag}-{index:04d}"
            occurred_at = _iso_now()
            payload: dict[str, Any] = {
                "msg_id": msg_id,
                "sent_at": occurred_at,
                "node_key": node_key,
                "event_type": "bench_event",
                "severity": "low",
                "occurred_at": occurred_at,
                "payload": {
                    "simulated": True,
                    "source": "bench_event_latency",
                    "sequence": index,
                },
            }
            sent[f"{node_key}:{msg_id}"] = time.time()
            client.publish(topic, json.dumps(payload), qos=1)
            if interval:
                target = start + (index + 1) * interval
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

        samples = await _collect(conn, tenant_id, sent, args.timeout)
        latencies = [value for _, value in samples]
        result = {
            "count": len(latencies),
            "rate_per_s": args.rate,
            "node_key": node_key,
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(_percentile(latencies, 0.95), 3),
            "p99_ms": round(_percentile(latencies, 0.99), 3),
            "min_ms": round(min(latencies), 3),
            "max_ms": round(max(latencies), 3),
            "missing": 0,
            "method": "MQTT publish -> vision_events.received_at",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if not args.keep:
            await conn.execute(
                "DELETE FROM vision_events WHERE tenant_id = $1 AND dedup_key = ANY($2::text[])",
                tenant_id,
                list(sent),
            )
            await conn.execute(
                "DELETE FROM edge_nodes WHERE tenant_id = $1 AND node_key = $2",
                tenant_id,
                node_key,
            )
        return 0
    finally:
        client.loop_stop()
        client.disconnect()
        await conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="事件端到端延迟基准")
    parser.add_argument("--count", type=int, default=100, help="事件条数，默认 100")
    parser.add_argument("--rate", type=float, default=20.0, help="每秒事件数，默认 20")
    parser.add_argument("--mqtt-host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    parser.add_argument("--database-url", default=DEFAULT_DB)
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT)
    parser.add_argument("--timeout", type=float, default=30.0, help="等待注册/落库秒数")
    parser.add_argument("--keep", action="store_true", help="保留基准事件与节点")
    args = parser.parse_args()
    if args.count <= 0:
        parser.error("--count 必须为正整数")
    if args.rate <= 0:
        parser.error("--rate 必须为正数")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(_parse_args())))
