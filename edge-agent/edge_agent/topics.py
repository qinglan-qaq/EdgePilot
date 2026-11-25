"""MQTT 主题集中定义（spec §9.1）。

约定：上行 ``edge/{node_key}/{register|heartbeat|status|event|task/ack}``，
下行 ``edge/{node_key}/cmd/{action}``。所有主题构造都经过 node_key 校验，
避免拼出通配符或空主题。
"""

from __future__ import annotations

_WILDCARDS = ("+", "#")


def _validate_node_key(node_key: str) -> None:
    if not isinstance(node_key, str) or not node_key.strip():
        raise ValueError("node_key 不能为空")
    if "/" in node_key:
        raise ValueError(f"node_key 不能包含 '/': {node_key!r}")
    if any(token in node_key for token in _WILDCARDS):
        raise ValueError(f"node_key 不能包含 MQTT 通配符: {node_key!r}")


def edge_topic(node_key: str, suffix: str) -> str:
    """构造 ``edge/{node_key}/{suffix}``。"""
    _validate_node_key(node_key)
    if not suffix or suffix.startswith("/"):
        raise ValueError(f"非法主题后缀: {suffix!r}")
    return f"edge/{node_key}/{suffix}"


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
    """订阅自身全部下行指令：``edge/{node_key}/cmd/+``。"""
    return edge_topic(node_key, "cmd/+")
