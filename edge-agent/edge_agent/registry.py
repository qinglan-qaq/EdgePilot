"""节点注册消息构建与发布（spec §9.1）。"""

from __future__ import annotations

import platform
import uuid
from datetime import datetime
from typing import Any, Callable

from .config import AGENT_VERSION
from .outbox import to_utc_iso, utcnow
from .topics import register_topic


def build_register_payload(
    node_key: str,
    *,
    os_name: str | None = None,
    arch: str | None = None,
    agent_version: str = AGENT_VERSION,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    """每次构建生成新 ``msg_id``；node/os/arch/version 稳定，服务端按 node_key 幂等更新。"""
    now = (clock or utcnow)()
    return {
        "msg_id": uuid.uuid4().hex,
        "sent_at": to_utc_iso(now),
        "node_key": node_key,
        "os": os_name if os_name is not None else (platform.system() or "unknown"),
        "arch": arch if arch is not None else (platform.machine() or "unknown"),
        "agent_version": agent_version,
    }


def register(mqtt: Any, payload: dict) -> bool:
    """发布注册消息：QoS 1，离线时写入 outbox 等重连补发。"""
    return mqtt.publish(
        register_topic(payload["node_key"]),
        payload,
        qos=1,
        retain=False,
        persist_on_failure=True,
    )
