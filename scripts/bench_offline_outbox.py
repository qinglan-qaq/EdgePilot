"""断网事件完整率基准（P7 Task 28）。

用 edge-agent 的真实 ``Outbox``（SQLite + WAL）模拟 5 分钟 broker 不可达：
两个节点各 5fps 共 3,000 条事件在 ``publish`` 失败后进入本地队列；恢复后
按 seq 顺序补发，统计完整率与重复率。

这是进程内等效演练，不启动真实 Mosquitto，也不等待 5 分钟墙钟；它验证的是
断网缓冲与补发语义，不替代真实网络断链演练。事件载荷显式标记 simulated。

运行::

    python scripts/bench_offline_outbox.py
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EDGE_AGENT_DIR = REPO_ROOT / "edge-agent"
if str(EDGE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(EDGE_AGENT_DIR))

from edge_agent.config import AgentConfig  # noqa: E402
from edge_agent.mqtt_client import MqttClient  # noqa: E402
from edge_agent.outbox import Outbox  # noqa: E402
from edge_agent.topics import event_topic  # noqa: E402


def _run(args: argparse.Namespace) -> int:
    expected = args.nodes * int(args.fps * args.seconds)
    delivered: list[str] = []
    duplicate_count = 0

    with tempfile.TemporaryDirectory(prefix="edgepilot-outbox-") as tmp:
        for node_index in range(1, args.nodes + 1):
            node_key = f"bench-offline-{node_index}"
            outbox = Outbox(Path(tmp) / f"{node_key}.sqlite3")
            config = AgentConfig(node_key=node_key, outbox_path=outbox.path)
            client = MqttClient(config, outbox)  # 不 connect：等价 broker 不可达
            try:
                per_node = int(args.fps * args.seconds)
                for index in range(per_node):
                    msg_id = f"{node_key}-{index:05d}"
                    accepted = client.publish(
                        event_topic(node_key),
                        {
                            "msg_id": msg_id,
                            "event_type": "bench_offline",
                            "severity": "low",
                            "occurred_at": "2026-01-01T00:00:00+00:00",
                            "payload": {
                                "simulated": True,
                                "source": "bench_offline_outbox",
                            },
                        },
                        qos=1,
                    )
                    if accepted:  # 未连接时 publish 必须返回 False 并落 outbox
                        raise RuntimeError("断网状态下 publish 不应返回成功")
                if outbox.count() != per_node:
                    raise RuntimeError(
                        f"{node_key} 断网缓冲数量不符：{outbox.count()} != {per_node}"
                    )

                # 恢复连接：真实实现由 on_connect 循环 flush 直到排空；
                # 这里复用同一语义，每次 flush 默认最多 100 条。
                while outbox.count():
                    result = outbox.flush(
                        lambda message: delivered.append(message.payload["msg_id"])
                    )
                    if result.error:
                        raise RuntimeError(f"补发失败：{result.error}")
                    if result.sent == 0:
                        raise RuntimeError("补发无进展，仍有待发消息")
                if outbox.count() != 0:
                    raise RuntimeError(f"{node_key} 补发后仍有 {outbox.count()} 条未发送")
            finally:
                client.disconnect()
                outbox.close()

    counts = Counter(delivered)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    unique_delivered = len(counts)
    completeness = unique_delivered / expected * 100.0 if expected else 0.0
    duplicate_rate = duplicate_count / expected * 100.0 if expected else 0.0

    result = {
        "mode": "outbox-equivalent",
        "outage_seconds": args.seconds,
        "nodes": args.nodes,
        "fps_per_node": args.fps,
        "expected": expected,
        "delivered": len(delivered),
        "unique_delivered": unique_delivered,
        "pending_after_flush": 0,
        "completeness_pct": round(completeness, 2),
        "duplicate_count": duplicate_count,
        "duplicate_rate_pct": round(duplicate_rate, 2),
        "method": "edge-agent Outbox：publish 失败缓冲 -> 按 seq flush",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if unique_delivered != expected or duplicate_count != 0:
        return 1
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="断网事件完整率基准（Outbox 等效演练）")
    parser.add_argument("--seconds", type=int, default=300, help="断网时长，默认 300 秒")
    parser.add_argument("--nodes", type=int, default=2, help="节点数，默认 2")
    parser.add_argument("--fps", type=float, default=5.0, help="每节点每秒事件数，默认 5")
    args = parser.parse_args()
    if args.seconds <= 0 or args.nodes <= 0 or args.fps <= 0:
        parser.error("--seconds/--nodes/--fps 必须为正数")
    return args


if __name__ == "__main__":
    raise SystemExit(_run(_parse_args()))
