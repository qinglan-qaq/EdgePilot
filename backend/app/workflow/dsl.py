"""工作流 DSL 的常量、节点/边辅助函数与稳定哈希。

本模块只做纯数据操作，不依赖 pydantic / 数据库，供编辑器即时校验、
发布去重与后续引擎复用。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

NODE_TYPES = ("start", "agent", "tool", "condition", "approval", "end")
ROLES = ("viewer", "operator", "admin")

# 节点 id 必须是合法标识符，便于表达式 ``$<node_id>.output`` 无歧义引用。
# 不允许下划线开头：表达式访问下划线字段会被安全白名单拒绝，避免出现
# "合法但永远无法被引用"的节点 id。
NODE_ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
NODE_ID_RE = re.compile(NODE_ID_PATTERN)
NODE_ID_MAX_LENGTH = 64
MAX_NODE_ID_LENGTH = NODE_ID_MAX_LENGTH
RESERVED_NODE_IDS = frozenset({"input"})


def is_valid_node_id(value: object) -> bool:
    """节点 id 是否满足标识符正则且不超过 64 字符。"""
    return (
        isinstance(value, str)
        and len(value) <= NODE_ID_MAX_LENGTH
        and NODE_ID_RE.fullmatch(value) is not None
    )


def nodes_of(dsl: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 ``dsl["nodes"]`` 中的对象条目；形状非法时返回空列表。"""
    raw = dsl.get("nodes") if isinstance(dsl, dict) else None
    if not isinstance(raw, list):
        return []
    return [node for node in raw if isinstance(node, dict)]


def edges_of(dsl: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 ``dsl["edges"]`` 中的对象条目；形状非法时返回空列表。"""
    raw = dsl.get("edges") if isinstance(dsl, dict) else None
    if not isinstance(raw, list):
        return []
    return [edge for edge in raw if isinstance(edge, dict)]


def node_map(dsl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按声明顺序建立 ``id -> node``；重复 id 保留首次出现。"""
    result: dict[str, dict[str, Any]] = {}
    for node in nodes_of(dsl):
        node_id = node.get("id")
        if isinstance(node_id, str):
            result.setdefault(node_id, node)
    return result


def dsl_hash(dsl: dict[str, Any]) -> str:
    """规范化 JSON 后的 SHA-256，供版本发布去重与幂等判断。

    键序、空白与中文转义都不影响结果；任何内容变化都会改变哈希。
    """
    canonical = json.dumps(
        dsl,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "MAX_NODE_ID_LENGTH",
    "NODE_ID_MAX_LENGTH",
    "NODE_ID_PATTERN",
    "NODE_ID_RE",
    "NODE_TYPES",
    "RESERVED_NODE_IDS",
    "ROLES",
    "dsl_hash",
    "edges_of",
    "is_valid_node_id",
    "node_map",
    "nodes_of",
]
