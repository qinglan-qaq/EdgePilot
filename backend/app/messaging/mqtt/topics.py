"""MQTT 主题解析与构造（spec §9.1）。

与 ``edge-agent/edge_agent/topics.py`` 保持同一套协议，但控制面还需按主题
反解 ``(node_key, kind)``。所有进出主题都先校验 node_key，避免拼出通配符或
空主题。
"""

from __future__ import annotations

EDGE_ROOT = "edge"
_WILDCARDS = ("+", "#")

# 上行后缀 -> 内部控制面消息类型
UPSTREAM_KINDS: dict[str, str] = {
    "register": "register",
    "heartbeat": "heartbeat",
    "status": "status",
    "event": "event",
    "task/ack": "task_ack",
}

COMMAND_ACTIONS = frozenset({"start_task", "stop_task", "probe_device", "sync_config"})


def is_valid_node_key(node_key: str) -> bool:
    if not isinstance(node_key, str) or not node_key.strip():
        return False
    if "/" in node_key:
        return False
    return not any(token in node_key for token in _WILDCARDS)


def validate_node_key(node_key: str) -> None:
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError("node_key 不能为空")
    if "/" in node_key:
        raise ValueError(f"node_key 不能包含 '/': {node_key!r}")
    if any(token in node_key for token in _WILDCARDS):
        raise ValueError(f"node_key 不能包含 MQTT 通配符: {node_key!r}")


def edge_topic(node_key: str, suffix: str) -> str:
    validate_node_key(node_key)
    if not suffix or suffix.startswith("/"):
        raise ValueError(f"非法主题后缀: {suffix!r}")
    return f"{EDGE_ROOT}/{node_key}/{suffix}"


def register_topic(node_key: str) -> str:
    return edge_topic(node_key, "register")


def heartbeat_topic(node_key: str) -> str:
    return edge_topic(node_key, "heartbeat")


def status_topic(node_key: str) -> str:
    return edge_topic(node_key, "status")


def event_topic(node_key: str) -> str:
    return edge_topic(node_key, "event")


def task_ack_topic(node_key: str) -> str:
    return edge_topic(node_key, "task/ack")


def cmd_topic(node_key: str, action: str) -> str:
    """下行指令主题 ``edge/{node_key}/cmd/{action}``。"""
    if not isinstance(action, str) or not action.strip() or "/" in action:
        raise ValueError(f"非法 cmd action: {action!r}")
    return edge_topic(node_key, f"cmd/{action}")


def cmd_filter(node_key: str) -> str:
    return edge_topic(node_key, "cmd/+")


def parse_uplink(topic: str) -> tuple[str, str] | None:
    """解析上行主题，返回 ``(node_key, kind)``；非法主题返回 ``None``。

    kind 取值见 :data:`UPSTREAM_KINDS` 的 value（``task_ack`` 而非 ``task/ack``）。
    """
    if not isinstance(topic, str):
        return None
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != EDGE_ROOT:
        return None
    node_key = parts[1]
    if not is_valid_node_key(node_key):
        return None
    suffix = "/".join(parts[2:])
    kind = UPSTREAM_KINDS.get(suffix)
    if kind is None:
        return None
    return node_key, kind


def parse_command(topic: str) -> tuple[str, str] | None:
    """解析 ``edge/{node_key}/cmd/{action}``，主要供测试/回放使用。"""
    if not isinstance(topic, str):
        return None
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != EDGE_ROOT or parts[2] != "cmd":
        return None
    node_key, action = parts[1], parts[3]
    if not is_valid_node_key(node_key):
        return None
    if not action or any(token in action for token in _WILDCARDS):
        return None
    return node_key, action


# 控制面订阅关系：register/status/event/task ack 走各实例独占，心跳走共享订阅。
UPSTREAM_SUBSCRIPTIONS: tuple[tuple[str, int], ...] = (
    (f"{EDGE_ROOT}/+/register", 1),
    ("$share/cp/" + f"{EDGE_ROOT}/+/heartbeat", 0),
    (f"{EDGE_ROOT}/+/status", 1),
    (f"{EDGE_ROOT}/+/event", 1),
    (f"{EDGE_ROOT}/+/task/ack", 1),
)
