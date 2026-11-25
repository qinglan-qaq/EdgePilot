"""模拟多节点：合成数据显式标记 ``simulated``，不冒充真实测量。"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import AGENT_VERSION, DEFAULT_HEARTBEAT_S, DEFAULT_MQTT_HOST, DEFAULT_MQTT_PORT, AgentConfig
from .heartbeat import HeartbeatPublisher
from .mqtt_client import MqttClient
from .outbox import Outbox, to_utc_iso, utcnow
from .registry import build_register_payload, register
from .topics import event_topic, status_topic

logger = logging.getLogger(__name__)

SIMULATION_MARKER = "simulated"


@dataclass(frozen=True)
class SimulatedNode:
    config: AgentConfig
    name: str
    simulated: bool = True

    @property
    def node_key(self) -> str:
        return self.config.node_key


def planned_simulated_nodes(
    count: int,
    *,
    mqtt_host: str = DEFAULT_MQTT_HOST,
    mqtt_port: int = DEFAULT_MQTT_PORT,
    heartbeat_s: float = DEFAULT_HEARTBEAT_S,
    agent_version: str = AGENT_VERSION,
) -> list[SimulatedNode]:
    """规划 N 个模拟节点（node_key 形如 ``sim-node-1``）。"""
    if count < 1:
        raise ValueError("模拟节点数必须 >= 1")
    nodes: list[SimulatedNode] = []
    for index in range(1, count + 1):
        config = AgentConfig(
            node_key=f"sim-node-{index}",
            mqtt_host=mqtt_host,
            mqtt_port=mqtt_port,
            heartbeat_s=heartbeat_s,
            agent_version=agent_version,
            simulate=True,
        )
        nodes.append(SimulatedNode(config=config, name=f"Simulated Node {index}"))
    return nodes


def simulated_event_payload(
    node_key: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    """构造明确标注为模拟的事件；不含任何伪造的检测置信度等业务数据。"""
    now = (clock or utcnow)()
    occurred_at = to_utc_iso(now)
    return {
        "msg_id": uuid.uuid4().hex,
        "sent_at": occurred_at,
        "node_key": node_key,
        SIMULATION_MARKER: True,
        "event_type": "mock_detection",
        "occurred_at": occurred_at,
        "message": "synthetic simulated event; not a real device reading",
    }


def run_simulated(
    nodes: list[SimulatedNode],
    *,
    stop_event: threading.Event | None = None,
    event_interval_s: float = 5.0,
) -> None:
    """运行模拟节点：注册 + 5s 心跳 + 周期模拟事件，直到 stop_event 置位。"""
    if not nodes:
        raise ValueError("至少需要一个模拟节点")
    stop = stop_event or threading.Event()
    runtimes: list[tuple[SimulatedNode, Outbox, MqttClient, threading.Thread]] = []

    try:
        for node in nodes:
            outbox = Outbox(node.config.outbox_path)
            client = MqttClient(node.config, outbox)
            try:
                client.connect()
                register(
                    client,
                    build_register_payload(
                        node.node_key, agent_version=node.config.agent_version
                    ),
                )
            except Exception:
                outbox.close()
                raise
            heartbeat = HeartbeatPublisher(
                client,
                node.node_key,
                interval_s=node.config.heartbeat_s,
                task_count_fn=lambda: 0,
            )
            thread = threading.Thread(
                target=heartbeat.run,
                args=(stop,),
                name=f"heartbeat-{node.node_key}",
                daemon=True,
            )
            thread.start()
            runtimes.append((node, outbox, client, thread))
            logger.info("模拟节点已启动 node_key=%s", node.node_key)

        while not stop.wait(event_interval_s):
            for node, _, client, _ in runtimes:
                client.publish(
                    event_topic(node.node_key),
                    simulated_event_payload(node.node_key),
                    qos=1,
                )
    finally:
        stop.set()
        for node, outbox, client, thread in runtimes:
            thread.join(timeout=2)
            client.publish(
                status_topic(node.node_key),
                "offline",
                qos=1,
                retain=True,
                persist_on_failure=False,
            )
            client.disconnect()
            outbox.close()
            logger.info("模拟节点已停止 node_key=%s", node.node_key)
