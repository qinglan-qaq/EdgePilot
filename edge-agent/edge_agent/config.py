"""edge-agent 配置：env + CLI 覆盖，frozen dataclass。"""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

AGENT_VERSION = "0.1.0"
DEFAULT_HEARTBEAT_S = 5.0
DEFAULT_MQTT_HOST = "localhost"
DEFAULT_MQTT_PORT = 1883


def default_node_key(hostname: str | None = None) -> str:
    """由主机名派生稳定 node_key；清洗到 MQTT 主题安全字符。"""
    raw = hostname if hostname is not None else socket.gethostname()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "-", raw or "").strip("-._").lower()
    return cleaned or "edge-node"


def _env_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AgentConfig:
    """单个边缘 agent 的运行配置。

    ``outbox_path`` 默认落在 ``var/edge_agent/<node_key>.sqlite3``；允许显式覆盖。
    """

    node_key: str
    mqtt_host: str = DEFAULT_MQTT_HOST
    mqtt_port: int = DEFAULT_MQTT_PORT
    heartbeat_s: float = DEFAULT_HEARTBEAT_S
    agent_version: str = AGENT_VERSION
    simulate: bool = False
    outbox_path: Path | None = field(default=None)

    def __post_init__(self) -> None:
        node_key = self.node_key.strip() if isinstance(self.node_key, str) else ""
        if not node_key:
            raise ValueError("node_key 不能为空")
        if any(ch in node_key for ch in ("/", "+", "#")):
            raise ValueError(f"node_key 含非法字符: {self.node_key!r}")
        if self.heartbeat_s <= 0:
            raise ValueError("heartbeat_s 必须为正数")
        if not 0 < int(self.mqtt_port) < 65536:
            raise ValueError(f"mqtt_port 越界: {self.mqtt_port}")
        outbox_path = (
            Path("var") / "edge_agent" / f"{node_key}.sqlite3"
            if self.outbox_path is None
            else Path(self.outbox_path)
        )
        object.__setattr__(self, "node_key", node_key)
        object.__setattr__(self, "heartbeat_s", float(self.heartbeat_s))
        object.__setattr__(self, "mqtt_port", int(self.mqtt_port))
        object.__setattr__(self, "outbox_path", outbox_path)


def load_config(
    *,
    node_key: str | None = None,
    mqtt_host: str | None = None,
    mqtt_port: int | None = None,
    heartbeat_s: float | None = None,
    outbox_path: str | Path | None = None,
    agent_version: str | None = None,
    simulate: bool | None = None,
    env: dict[str, str] | None = None,
) -> AgentConfig:
    """从环境变量读取配置，并用显式参数（CLI）覆盖。"""
    source = os.environ if env is None else env

    resolved_node_key = node_key or source.get("EDGE_NODE_KEY") or default_node_key()
    resolved_host = mqtt_host or source.get("MQTT_HOST") or DEFAULT_MQTT_HOST
    resolved_port = (
        mqtt_port
        if mqtt_port is not None
        else int(source.get("MQTT_PORT") or DEFAULT_MQTT_PORT)
    )
    resolved_heartbeat = (
        heartbeat_s
        if heartbeat_s is not None
        else float(source.get("EDGE_HEARTBEAT_S") or DEFAULT_HEARTBEAT_S)
    )
    resolved_path = outbox_path if outbox_path is not None else source.get("EDGE_OUTBOX_PATH")
    resolved_version = agent_version or source.get("EDGE_AGENT_VERSION") or AGENT_VERSION

    if simulate is None:
        simulate = _env_bool(source.get("EDGE_SIMULATE")) or False

    return AgentConfig(
        node_key=resolved_node_key,
        mqtt_host=resolved_host,
        mqtt_port=resolved_port,
        heartbeat_s=resolved_heartbeat,
        agent_version=resolved_version,
        simulate=simulate,
        outbox_path=resolved_path,
    )
