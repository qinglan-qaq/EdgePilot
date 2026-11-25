"""工作流 DSL 校验器：纯结构校验 + 数据库内的 Agent/Tool 可用性校验。

设计约定：

- ``validate_dsl`` 是纯函数，报告式返回，不抛结构化异常；编辑器可只做结构
  校验（不传 ``known_agents`` / ``known_tools``）。
- ``validate_workflow`` 先做结构校验，结构无错误时才查询同租户的 active
  Agent 与 enabled Tool；查询只读、不 commit、不跨租户。
- ``assert_valid`` 是发布/触发路径的入口，无效时抛 400 ``AppError``。
"""

from __future__ import annotations

import heapq
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError
from app.models.agent import Agent
from app.models.tool import Tool
from app.workflow.dsl import (
    NODE_ID_MAX_LENGTH,
    NODE_ID_RE,
    NODE_TYPES,
    RESERVED_NODE_IDS,
    ROLES,
)
from app.workflow.expression import ExpressionError, compile_expression

MAX_TIMEOUT_MS = 86_400_000

_SCALAR_TYPES = (str, int, float, bool, type(None))


@dataclass
class ValidationIssue:
    """单条校验问题；``node_id`` 为 None 表示图级/顶层问题。"""

    code: str
    message: str
    node_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "node_id": self.node_id,
        }


@dataclass
class ValidationReport:
    """一次校验的完整结果；``valid`` 只取决于 ``errors`` 是否为空。"""

    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    entry_node: str | None = None
    end_nodes: list[str] = field(default_factory=list)
    topological_order: list[str] | None = None

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def errors_as_dicts(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.errors]

    def add_error(
        self, code: str, message: str, node_id: str | None = None
    ) -> None:
        self.errors.append(ValidationIssue(code=code, message=message, node_id=node_id))

    def add_warning(
        self, code: str, message: str, node_id: str | None = None
    ) -> None:
        self.warnings.append(
            ValidationIssue(code=code, message=message, node_id=node_id)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors_as_dicts,
            "warnings": [issue.to_dict() for issue in self.warnings],
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entry_node": self.entry_node,
            "end_nodes": list(self.end_nodes),
            "topological_order": (
                list(self.topological_order)
                if self.topological_order is not None
                else None
            ),
        }


class WorkflowValidationError(AppError):
    """工作流 DSL 校验失败；``detail.errors`` 为问题列表。"""

    code = 400
    http_status = 400


def validate_dsl(
    dsl: Any,
    *,
    known_agents: set[str] | None = None,
    known_tools: set[str] | None = None,
) -> ValidationReport:
    """校验 DSL 的结构与节点字段；可选校验 Agent/Tool 是否存在且启用。"""
    report = ValidationReport()

    if not isinstance(dsl, dict):
        report.add_error("dsl_not_object", "DSL 必须是 JSON 对象")
        return report

    raw_nodes = dsl.get("nodes")
    if not isinstance(raw_nodes, list):
        report.add_error("invalid_nodes", "nodes 必须是非空数组")
        return report
    report.node_count = len(raw_nodes)
    if not raw_nodes:
        report.add_error("nodes_empty", "工作流至少需要一个节点")
        return report

    raw_edges = dsl.get("edges")
    if not isinstance(raw_edges, list):
        report.add_error("invalid_edges", "edges 必须是数组")
        return report
    report.edge_count = len(raw_edges)

    nodes = _validate_node_fields(report, raw_nodes)
    node_by_id = _build_node_map(nodes)
    ordered_ids = list(node_by_id)

    start_ids = [
        node_id
        for node_id, node in node_by_id.items()
        if node.get("type") == "start"
    ]
    end_ids = [
        node_id
        for node_id, node in node_by_id.items()
        if node.get("type") == "end"
    ]
    _validate_start_end(report, start_ids, end_ids)

    graph = _validate_edges(report, raw_edges, node_by_id, ordered_ids)
    _validate_condition_branches(report, node_by_id, graph["condition_edges"])
    _validate_start_end_degrees(report, start_ids, end_ids, graph)

    indegree = graph["indegree"]
    adjacency = graph["adjacency"]
    processed = _kahn_processed(ordered_ids, indegree, adjacency)
    if len(processed) == len(ordered_ids):
        report.topological_order = processed
    else:
        processed_set = set(processed)
        stuck = [
            node_id for node_id in ordered_ids if node_id not in processed_set
        ]
        report.add_error(
            "cycle_detected",
            "工作流存在环，涉及节点: " + ", ".join(stuck),
        )

    _validate_reachability(
        report,
        ordered_ids,
        adjacency,
        graph["reverse"],
        start_ids,
        end_ids,
    )
    _validate_node_semantics(report, nodes)
    _validate_expression_references(
        report,
        nodes,
        node_by_id,
        graph["reverse"],
    )
    _validate_known_refs(report, nodes, known_agents, known_tools)

    return report


# --------------------------------------------------------------- 节点字段校验


def _issue_node_id(raw: dict[str, Any]) -> str | None:
    node_id = raw.get("id")
    return node_id if isinstance(node_id, str) else None


def _validate_node_fields(
    report: ValidationReport, raw_nodes: list[Any]
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for position, raw in enumerate(raw_nodes, start=1):
        if not isinstance(raw, dict):
            report.add_error(
                "invalid_node", f"第 {position} 个节点必须是 JSON 对象"
            )
            continue
        nodes.append(raw)
        node_id = _issue_node_id(raw)

        if "id" not in raw:
            report.add_error(
                "missing_node_id", f"第 {position} 个节点缺少 id"
            )
        elif node_id is None:
            report.add_error("invalid_node_id", "节点 id 必须是字符串")
        else:
            if (
                len(node_id) > NODE_ID_MAX_LENGTH
                or NODE_ID_RE.fullmatch(node_id) is None
            ):
                report.add_error(
                    "invalid_node_id",
                    f"节点 id 非法（需匹配 ^[A-Za-z][A-Za-z0-9_]*$ 且不超过 "
                    f"{NODE_ID_MAX_LENGTH} 字符）: {node_id}",
                    node_id,
                )
            elif node_id in RESERVED_NODE_IDS:
                report.add_error(
                    "reserved_node_id",
                    f"节点 id {node_id!r} 为保留字，无法在表达式中引用",
                    node_id,
                )
            if node_id in seen_ids:
                report.add_error(
                    "duplicate_node_id", f"节点 id 重复: {node_id}", node_id
                )
            else:
                seen_ids.add(node_id)

        node_type = raw.get("type")
        if "type" not in raw:
            report.add_error(
                "missing_node_type",
                f"节点 {node_id or position} 缺少 type",
                node_id,
            )
        elif node_type not in NODE_TYPES:
            report.add_error(
                "unknown_node_type",
                f"未知节点类型: {node_type!r}；允许值为 {', '.join(NODE_TYPES)}",
                node_id,
            )

        if "timeout_ms" not in raw:
            report.add_error(
                "missing_timeout",
                f"节点 {node_id or position} 缺少 timeout_ms",
                node_id,
            )
        else:
            timeout = raw["timeout_ms"]
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, int)
                or not 1 <= timeout <= MAX_TIMEOUT_MS
            ):
                report.add_error(
                    "invalid_timeout",
                    f"节点 {node_id or position} 的 timeout_ms 必须是 1.."
                    f"{MAX_TIMEOUT_MS} 的整数",
                    node_id,
                )

    return nodes


def _build_node_map(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = node.get("id")
        if isinstance(node_id, str):
            result.setdefault(node_id, node)
    return result


# --------------------------------------------------------------- 起止节点校验


def _validate_start_end(
    report: ValidationReport, start_ids: list[str], end_ids: list[str]
) -> None:
    if not start_ids:
        report.add_error("missing_start", "工作流必须有且仅有一个 start 节点")
    elif len(start_ids) > 1:
        report.add_error(
            "duplicate_start",
            "工作流只能有一个 start 节点，实际为: " + ", ".join(start_ids),
        )
    else:
        report.entry_node = start_ids[0]

    if not end_ids:
        report.add_error("missing_end", "工作流至少需要一个 end 节点")
    else:
        report.end_nodes = list(end_ids)


# ------------------------------------------------------------------- 边校验


def _validate_edges(
    report: ValidationReport,
    raw_edges: list[Any],
    node_by_id: dict[str, dict[str, Any]],
    ordered_ids: list[str],
) -> dict[str, Any]:
    indegree = {node_id: 0 for node_id in ordered_ids}
    outdegree = {node_id: 0 for node_id in ordered_ids}
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in ordered_ids}
    reverse: dict[str, list[str]] = {node_id: [] for node_id in ordered_ids}
    condition_edges: dict[str, dict[str, int]] = {
        node_id: {"true": 0, "false": 0}
        for node_id, node in node_by_id.items()
        if node.get("type") == "condition"
    }
    seen_pairs: set[tuple[str, str]] = set()

    for position, raw in enumerate(raw_edges, start=1):
        if not isinstance(raw, dict):
            report.add_error(
                "invalid_edge", f"第 {position} 条边必须是 JSON 对象"
            )
            continue

        source = raw.get("from")
        target = raw.get("to")
        if not isinstance(source, str) or not isinstance(target, str):
            report.add_error(
                "invalid_edge",
                f"第 {position} 条边必须包含字符串 from 与 to",
            )
            continue

        if source not in node_by_id:
            report.add_error(
                "unknown_edge_node",
                f"边起点不存在: {source}",
                source,
            )
        if target not in node_by_id:
            report.add_error(
                "unknown_edge_node",
                f"边终点不存在: {target}",
                target,
            )

        if source == target:
            report.add_error(
                "self_loop", f"节点 {source} 不允许自环", source
            )

        pair = (source, target)
        if pair in seen_pairs:
            report.add_error(
                "duplicate_edge",
                f"重复边: {source} -> {target}",
                source,
            )
        else:
            seen_pairs.add(pair)

        source_node = node_by_id.get(source)
        if source_node is not None:
            source_type = source_node.get("type")
            has_when = "when" in raw
            when = raw.get("when")
            if source_type == "condition":
                if not has_when:
                    report.add_error(
                        "missing_when",
                        f"condition 节点 {source} 的出边必须带 when",
                        source,
                    )
                elif when not in ("true", "false"):
                    report.add_error(
                        "invalid_when",
                        f"condition 节点 {source} 的 when 只能是 "
                        f"'true' 或 'false'，实际为 {when!r}",
                        source,
                    )
                else:
                    condition_edges[source][when] += 1
            elif has_when:
                report.add_error(
                    "unexpected_when",
                    f"非 condition 节点 {source} 的出边不允许带 when",
                    source,
                )

        if source in node_by_id and target in node_by_id:
            indegree[target] += 1
            outdegree[source] += 1
            adjacency[source].append(target)
            reverse[target].append(source)

    return {
        "indegree": indegree,
        "outdegree": outdegree,
        "adjacency": adjacency,
        "reverse": reverse,
        "condition_edges": condition_edges,
    }


def _validate_condition_branches(
    report: ValidationReport,
    node_by_id: dict[str, dict[str, Any]],
    condition_edges: dict[str, dict[str, int]],
) -> None:
    for node_id in node_by_id:
        if node_id not in condition_edges:
            continue
        counts = condition_edges[node_id]
        if counts["true"] != 1 or counts["false"] != 1:
            report.add_error(
                "invalid_condition_branches",
                f"condition 节点 {node_id} 必须恰好各有一条 when='true' 与 "
                f"when='false' 出边，实际 true={counts['true']}, "
                f"false={counts['false']}",
                node_id,
            )


def _validate_start_end_degrees(
    report: ValidationReport,
    start_ids: list[str],
    end_ids: list[str],
    graph: dict[str, Any],
) -> None:
    indegree = graph["indegree"]
    outdegree = graph["outdegree"]

    if len(start_ids) == 1:
        start = start_ids[0]
        if indegree.get(start, 0) > 0:
            report.add_error(
                "start_has_incoming",
                f"start 节点 {start} 不允许有入边",
                start,
            )
        if outdegree.get(start, 0) != 1:
            report.add_error(
                "start_outgoing_degree",
                f"start 节点 {start} 必须恰好有 1 条出边，实际为 "
                f"{outdegree.get(start, 0)}",
                start,
            )

    for end in end_ids:
        if outdegree.get(end, 0) > 0:
            report.add_error(
                "end_has_outgoing",
                f"end 节点 {end} 不允许有出边",
                end,
            )


# --------------------------------------------------------------- 拓扑与可达性


def _kahn_processed(
    ordered_ids: list[str],
    indegree: dict[str, int],
    adjacency: dict[str, list[str]],
) -> list[str]:
    """稳定 Kahn：可用节点按声明顺序（堆 key 为声明下标）出队。"""
    position = {node_id: index for index, node_id in enumerate(ordered_ids)}
    remaining = {node_id: indegree.get(node_id, 0) for node_id in ordered_ids}
    heap = [
        (position[node_id], node_id)
        for node_id in ordered_ids
        if remaining[node_id] == 0
    ]
    heapq.heapify(heap)

    processed: list[str] = []
    while heap:
        _, node_id = heapq.heappop(heap)
        processed.append(node_id)
        for next_id in adjacency.get(node_id, []):
            if next_id not in remaining:
                continue
            remaining[next_id] -= 1
            if remaining[next_id] == 0:
                heapq.heappush(heap, (position[next_id], next_id))
    return processed


def _validate_reachability(
    report: ValidationReport,
    ordered_ids: list[str],
    adjacency: dict[str, list[str]],
    reverse: dict[str, list[str]],
    start_ids: list[str],
    end_ids: list[str],
) -> None:
    if len(start_ids) == 1:
        start = start_ids[0]
        reachable = _walk(start, adjacency)
        unreachable = [
            node_id for node_id in ordered_ids if node_id not in reachable
        ]
        if unreachable:
            report.add_error(
                "unreachable_nodes",
                "以下节点无法从 start 到达: " + ", ".join(unreachable),
            )

    if end_ids:
        can_reach_end: set[str] = set()
        for end in end_ids:
            can_reach_end |= _walk(end, reverse)
        dead_ends = [
            node_id for node_id in ordered_ids if node_id not in can_reach_end
        ]
        if dead_ends:
            report.add_error(
                "dead_end_nodes",
                "以下节点无法到达任何 end: " + ", ".join(dead_ends),
            )


def _walk(root: str, adjacency: dict[str, list[str]]) -> set[str]:
    visited: set[str] = set()
    stack = [root]
    while stack:
        node_id = stack.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        stack.extend(adjacency.get(node_id, []))
    return visited


# ------------------------------------------------------------- 节点语义校验


def _validate_node_semantics(
    report: ValidationReport, nodes: list[dict[str, Any]]
) -> None:
    for node in nodes:
        node_id = _issue_node_id(node)
        node_type = node.get("type")

        if node_type == "agent":
            agent_name = node.get("agent")
            if not isinstance(agent_name, str) or not agent_name.strip():
                report.add_error(
                    "invalid_agent_node",
                    f"agent 节点 {node_id} 必须包含非空 agent 名称",
                    node_id,
                )

        elif node_type == "tool":
            tool_id = node.get("tool")
            if not isinstance(tool_id, str) or not tool_id.strip():
                report.add_error(
                    "invalid_tool_node",
                    f"tool 节点 {node_id} 必须包含非空 tool 标识",
                    node_id,
                )

        elif node_type == "condition":
            expression = node.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                report.add_error(
                    "invalid_expression",
                    f"condition 节点 {node_id} 必须包含非空 expression",
                    node_id,
                )
            else:
                try:
                    compile_expression(expression)
                except ExpressionError as exc:
                    report.add_error(
                        "invalid_expression",
                        f"condition 节点 {node_id} 的 expression 非法: "
                        f"{exc.message}",
                        node_id,
                    )

        elif node_type == "approval":
            if not _valid_approval(node):
                report.add_error(
                    "invalid_approval_node",
                    f"approval 节点 {node_id} 必须包含非空 title 与非空 "
                    f"approvers 列表（仅允许 {', '.join(ROLES)}）",
                    node_id,
                )

        if node_type in ("agent", "tool"):
            _validate_input_map(report, node_id, node)


def _validate_expression_references(
    report: ValidationReport,
    nodes: list[dict[str, Any]],
    node_by_id: dict[str, dict[str, Any]],
    reverse: dict[str, list[str]],
) -> None:
    """表达式只能读取已存在且位于当前节点上游的节点输出。

    没有任何执行顺序保证的跨分支引用会在运行时拿到空上下文，因此在
    发布前直接拒绝；``$input`` 不受此限制。
    """
    for node in nodes:
        node_id = _issue_node_id(node)
        if node_id is None or node_id not in node_by_id:
            continue
        ancestors = _walk(node_id, reverse) - {node_id}
        seen: set[str] = set()
        for source, field in _iter_node_expressions(node):
            try:
                compiled = compile_expression(source)
            except ExpressionError:
                continue
            for ref in compiled.referenced_nodes:
                if ref in seen:
                    continue
                seen.add(ref)
                if ref not in node_by_id:
                    report.add_error(
                        "unknown_expression_node",
                        f"节点 {node_id} 的 {field} 引用了不存在的节点 {ref!r}",
                        node_id,
                    )
                elif ref not in ancestors:
                    report.add_error(
                        "forward_expression_reference",
                        f"节点 {node_id} 的 {field} 只能引用上游节点，"
                        f"{ref!r} 不是其上游",
                        node_id,
                    )


def _iter_node_expressions(node: dict[str, Any]) -> Iterator[tuple[str, str]]:
    node_type = node.get("type")
    if node_type == "condition":
        expression = node.get("expression")
        if isinstance(expression, str):
            yield expression, "expression"
    elif node_type in ("agent", "tool"):
        input_map = node.get("input_map")
        if isinstance(input_map, dict):
            for key, value in input_map.items():
                if isinstance(value, str) and value.startswith("$"):
                    yield value, f"input_map[{key!r}]"


def _valid_approval(node: dict[str, Any]) -> bool:
    title = node.get("title")
    approvers = node.get("approvers")
    if not isinstance(title, str) or not title.strip():
        return False
    if not isinstance(approvers, list) or not approvers:
        return False
    return all(
        isinstance(role, str) and role in ROLES for role in approvers
    )


def _validate_input_map(
    report: ValidationReport, node_id: str | None, node: dict[str, Any]
) -> None:
    if "input_map" not in node:
        return
    input_map = node["input_map"]
    if not isinstance(input_map, dict) or not all(
        isinstance(key, str) for key in input_map
    ):
        report.add_error(
            "invalid_input_map",
            f"节点 {node_id} 的 input_map 必须是 dict[str, Any]",
            node_id,
        )
        return

    for key, value in input_map.items():
        if type(value) not in _SCALAR_TYPES:
            report.add_error(
                "invalid_input_map",
                f"节点 {node_id} 的 input_map[{key!r}] 只能是标量或字符串，"
                f"不允许 list/dict",
                node_id,
            )
            continue
        if isinstance(value, str) and value.startswith("$"):
            try:
                compile_expression(value)
            except ExpressionError as exc:
                report.add_error(
                    "invalid_input_map_expression",
                    f"节点 {node_id} 的 input_map[{key!r}] 表达式非法: "
                    f"{exc.message}",
                    node_id,
                )


def _validate_known_refs(
    report: ValidationReport,
    nodes: list[dict[str, Any]],
    known_agents: set[str] | None,
    known_tools: set[str] | None,
) -> None:
    if known_agents is not None:
        for node in nodes:
            if node.get("type") != "agent":
                continue
            agent_name = node.get("agent")
            if (
                isinstance(agent_name, str)
                and agent_name.strip()
                and agent_name not in known_agents
            ):
                report.add_error(
                    "agent_not_found_or_disabled",
                    f"agent 节点 {_issue_node_id(node)} 引用的 Agent "
                    f"{agent_name!r} 不存在或未启用",
                    _issue_node_id(node),
                )

    if known_tools is not None:
        for node in nodes:
            if node.get("type") != "tool":
                continue
            tool_id = node.get("tool")
            if (
                isinstance(tool_id, str)
                and tool_id.strip()
                and tool_id not in known_tools
            ):
                report.add_error(
                    "tool_not_found_or_disabled",
                    f"tool 节点 {_issue_node_id(node)} 引用的 Tool "
                    f"{tool_id!r} 不存在或未启用",
                    _issue_node_id(node),
                )


# --------------------------------------------------------------- 发布入口


def assert_valid(report: ValidationReport) -> None:
    """报告无效时抛 400；有效时直接返回。"""
    if report.valid:
        return
    raise WorkflowValidationError(
        "工作流 DSL 校验失败",
        detail={"errors": report.errors_as_dicts},
    )


async def validate_workflow(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    dsl: dict[str, Any],
) -> ValidationReport:
    """结构校验通过后，用同租户 active Agent / enabled Tool 再校验引用。"""
    report = validate_dsl(dsl)
    if report.errors:
        return report

    agent_names = set(
        (
            await session.scalars(
                select(Agent.name).where(
                    Agent.tenant_id == tenant_id,
                    Agent.status == "active",
                )
            )
        ).all()
    )
    tool_ids = set(
        (
            await session.scalars(
                select(Tool.tool_id).where(
                    Tool.tenant_id == tenant_id,
                    Tool.enabled.is_(True),
                )
            )
        ).all()
    )
    return validate_dsl(
        dsl, known_agents=agent_names, known_tools=tool_ids
    )


__all__ = [
    "MAX_TIMEOUT_MS",
    "ValidationIssue",
    "ValidationReport",
    "WorkflowValidationError",
    "assert_valid",
    "validate_dsl",
    "validate_workflow",
]
