"""工作流 DSL、安全表达式与校验器的纯逻辑测试（Task 21）。

这些用例不碰数据库：合法/非法图结构、表达式白名单与求值语义都在进程内完成。
"""

from __future__ import annotations

import copy
import json

import pytest

from app.workflow.dsl import dsl_hash, edges_of, node_map
from app.workflow.expression import (
    ExpressionError,
    compile_expression,
    evaluate_condition,
    evaluate_expression,
)
from app.workflow.validator import (
    ValidationReport,
    WorkflowValidationError,
    assert_valid,
    validate_dsl,
)


# --------------------------------------------------------------------------- 夹具


def six_node_dsl() -> dict:
    """规格 §7.1 的 6 节点示例：start→agent→condition→approval→agent→end。"""
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "plan",
                "type": "agent",
                "agent": "planner",
                "timeout_ms": 30000,
                "input_map": {"question": "$input.question"},
            },
            {
                "id": "has_risk",
                "type": "condition",
                "expression": "$plan.output.high_risk_count > 0",
                "timeout_ms": 5000,
            },
            {
                "id": "approve",
                "type": "approval",
                "title": "高风险事件需确认",
                "approvers": ["operator", "admin"],
                "timeout_ms": 86400000,
            },
            {
                "id": "report",
                "type": "agent",
                "agent": "reporter",
                "timeout_ms": 30000,
                "input_map": {"events": "$plan.output.events"},
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "plan"},
            {"from": "plan", "to": "has_risk"},
            {"from": "has_risk", "to": "approve", "when": "true"},
            {"from": "has_risk", "to": "end", "when": "false"},
            {"from": "approve", "to": "report"},
            {"from": "report", "to": "end"},
        ],
    }


def minimal_dsl(node: dict, *, end_before: bool = False) -> dict:
    """start→node→end 的最小图，方便单独验证某类节点。"""
    nodes = [
        {"id": "start", "type": "start", "timeout_ms": 1000},
        node,
        {"id": "end", "type": "end", "timeout_ms": 1000},
    ]
    edges = [
        {"from": "start", "to": node["id"]},
        {"from": node["id"], "to": "end"},
    ]
    return {"nodes": nodes, "edges": edges}


def tool_dsl(tool_id: str = "query_device_status") -> dict:
    return minimal_dsl(
        {
            "id": "lookup",
            "type": "tool",
            "tool": tool_id,
            "timeout_ms": 3000,
        }
    )


def codes(report: ValidationReport) -> set[str]:
    return {issue.code for issue in report.errors}


def issue_for(report: ValidationReport, code: str):
    matches = [issue for issue in report.errors if issue.code == code]
    assert matches, f"未找到错误 {code}，实际为 {codes(report)}"
    return matches[0]


# ----------------------------------------------------------------- 合法图与辅助函数


def test_six_node_example_is_valid():
    report = validate_dsl(
        six_node_dsl(), known_agents={"planner", "reporter"}
    )

    assert report.valid is True
    assert report.errors == []
    assert report.node_count == 6
    assert report.edge_count == 6
    assert report.entry_node == "start"
    assert report.end_nodes == ["end"]
    assert report.topological_order is not None
    assert set(report.topological_order) == {
        "start",
        "plan",
        "has_risk",
        "approve",
        "report",
        "end",
    }
    assert report.topological_order[0] == "start"
    assert report.topological_order[-1] == "end"


def test_report_to_dict_is_json_serializable():
    report = validate_dsl(six_node_dsl())

    payload = report.to_dict()
    encoded = json.dumps(payload, ensure_ascii=False)

    assert "start" in encoded
    assert payload["valid"] is True
    assert payload["node_count"] == 6
    assert payload["errors"] == []


def test_node_map_and_edges_of_ignore_malformed_entries():
    assert node_map(six_node_dsl())["plan"]["type"] == "agent"
    assert edges_of(six_node_dsl())[0] == {"from": "start", "to": "plan"}
    assert node_map({"nodes": "bad"}) == {}
    assert edges_of({"edges": "bad"}) == []


def test_dsl_hash_ignores_key_order_but_tracks_content():
    left = six_node_dsl()
    right = {key: copy.deepcopy(value) for key, value in reversed(list(left.items()))}

    assert dsl_hash(left) == dsl_hash(right)
    assert len(dsl_hash(left)) == 64

    right["nodes"][1]["agent"] = "other-planner"
    assert dsl_hash(left) != dsl_hash(right)


def test_dsl_hash_uses_compact_non_ascii_json():
    first = {"name": "设备", "value": 1}
    second = {"value": 1, "name": "设备"}

    assert dsl_hash(first) == dsl_hash(second)


# ---------------------------------------------------------------------- 结构错误


def test_cycle_detected():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "report", "to": "plan"})

    report = validate_dsl(dsl)

    assert report.valid is False
    issue_for(report, "cycle_detected")
    assert report.topological_order is None


def test_unreachable_node():
    dsl = six_node_dsl()
    dsl["nodes"].append({"id": "lonely", "type": "end", "timeout_ms": 1000})

    report = validate_dsl(dsl)

    issue = issue_for(report, "unreachable_nodes")
    assert "lonely" in issue.message


def test_node_cannot_reach_end():
    dsl = minimal_dsl({"id": "a", "type": "agent", "agent": "planner", "timeout_ms": 1000})
    dsl["nodes"].append({"id": "sink", "type": "end", "timeout_ms": 1000})
    dsl["edges"].append({"from": "a", "to": "sink"})
    # sink 自身是 end，但 a 仍能到 end；再造一个真正无出口的 agent。
    dsl["nodes"].append({"id": "dangling", "type": "agent", "agent": "planner", "timeout_ms": 1000})
    dsl["edges"].append({"from": "a", "to": "dangling"})

    report = validate_dsl(dsl)

    issue = issue_for(report, "dead_end_nodes")
    assert "dangling" in issue.message


def test_duplicate_start():
    dsl = six_node_dsl()
    dsl["nodes"].append({"id": "start2", "type": "start", "timeout_ms": 1000})

    report = validate_dsl(dsl)

    issue_for(report, "duplicate_start")


def test_missing_start():
    dsl = six_node_dsl()
    dsl["nodes"] = [node for node in dsl["nodes"] if node["type"] != "start"]
    dsl["edges"] = [edge for edge in dsl["edges"] if edge["from"] != "start"]

    report = validate_dsl(dsl)

    issue_for(report, "missing_start")


def test_missing_end():
    dsl = six_node_dsl()
    dsl["nodes"] = [node for node in dsl["nodes"] if node["type"] != "end"]
    dsl["edges"] = [edge for edge in dsl["edges"] if edge["to"] != "end"]

    report = validate_dsl(dsl)

    issue_for(report, "missing_end")


def test_duplicate_node_id():
    dsl = six_node_dsl()
    dsl["nodes"].append(
        {"id": "plan", "type": "agent", "agent": "planner", "timeout_ms": 1000}
    )

    report = validate_dsl(dsl)

    issue = issue_for(report, "duplicate_node_id")
    assert issue.node_id == "plan"


def test_unknown_node_type():
    dsl = six_node_dsl()
    dsl["nodes"][1]["type"] = "llm"

    report = validate_dsl(dsl)

    issue_for(report, "unknown_node_type")


def test_missing_timeout():
    dsl = six_node_dsl()
    dsl["nodes"][1].pop("timeout_ms")

    report = validate_dsl(dsl)

    assert issue_for(report, "missing_timeout").node_id == "plan"


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 86400001, "1000", 1.5, None])
def test_invalid_timeout(timeout):
    dsl = six_node_dsl()
    dsl["nodes"][1]["timeout_ms"] = timeout

    report = validate_dsl(dsl)

    issue_for(report, "invalid_timeout")


def test_edge_points_to_unknown_node():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "report", "to": "ghost"})

    report = validate_dsl(dsl)

    issue_for(report, "unknown_edge_node")


def test_self_loop():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "plan", "to": "plan"})

    report = validate_dsl(dsl)

    issue_for(report, "self_loop")


def test_duplicate_edge():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "start", "to": "plan"})

    report = validate_dsl(dsl)

    issue_for(report, "duplicate_edge")


def test_start_has_incoming_edge():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "report", "to": "start"})

    report = validate_dsl(dsl)

    issue_for(report, "start_has_incoming")


def test_end_has_outgoing_edge():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "end", "to": "report"})

    report = validate_dsl(dsl)

    issue_for(report, "end_has_outgoing")


def test_start_must_have_exactly_one_outgoing_edge():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "start", "to": "end"})

    report = validate_dsl(dsl)

    issue_for(report, "start_outgoing_degree")


def test_condition_missing_true_branch():
    dsl = six_node_dsl()
    dsl["edges"] = [
        edge
        for edge in dsl["edges"]
        if not (
            edge["from"] == "has_risk"
            and edge["to"] == "approve"
            and edge.get("when") == "true"
        )
    ]

    report = validate_dsl(dsl)

    issue = issue_for(report, "invalid_condition_branches")
    assert issue.node_id == "has_risk"


def test_condition_missing_false_branch():
    dsl = six_node_dsl()
    dsl["edges"] = [
        edge
        for edge in dsl["edges"]
        if not (edge["from"] == "has_risk" and edge.get("when") == "false")
    ]

    report = validate_dsl(dsl)

    issue_for(report, "invalid_condition_branches")


def test_condition_duplicate_true_branch():
    dsl = six_node_dsl()
    dsl["edges"].append({"from": "has_risk", "to": "report", "when": "true"})

    report = validate_dsl(dsl)

    issue_for(report, "invalid_condition_branches")


def test_condition_invalid_when_value():
    dsl = six_node_dsl()
    for edge in dsl["edges"]:
        if edge["from"] == "has_risk" and edge.get("when") == "true":
            edge["when"] = "yes"

    report = validate_dsl(dsl)

    issue_for(report, "invalid_when")


def test_non_condition_edge_with_when():
    dsl = six_node_dsl()
    dsl["edges"][0]["when"] = "true"

    report = validate_dsl(dsl)

    issue = issue_for(report, "unexpected_when")
    assert issue.node_id == "start"


def test_condition_edge_without_when():
    dsl = six_node_dsl()
    for edge in dsl["edges"]:
        if edge["from"] == "has_risk" and edge.get("when") == "true":
            edge.pop("when")

    report = validate_dsl(dsl)

    issue_for(report, "missing_when")


# ------------------------------------------------------------------ 节点字段错误


def test_agent_node_requires_agent_name():
    dsl = six_node_dsl()
    dsl["nodes"][1]["agent"] = ""

    report = validate_dsl(dsl)

    issue_for(report, "invalid_agent_node")


def test_tool_node_requires_tool_id():
    dsl = tool_dsl("")
    dsl["nodes"][1]["tool"] = ""

    report = validate_dsl(dsl)

    issue_for(report, "invalid_tool_node")


def test_condition_requires_compilable_expression():
    dsl = six_node_dsl()
    dsl["nodes"][2]["expression"] = "$plan.output.__class__"

    report = validate_dsl(dsl)

    issue = issue_for(report, "invalid_expression")
    assert issue.node_id == "has_risk"


def test_condition_requires_non_empty_expression():
    dsl = six_node_dsl()
    dsl["nodes"][2]["expression"] = "   "

    report = validate_dsl(dsl)

    issue_for(report, "invalid_expression")


def test_approval_requires_title_and_valid_roles():
    dsl = six_node_dsl()
    dsl["nodes"][3]["title"] = ""
    dsl["nodes"][3]["approvers"] = ["operator", "superuser"]

    report = validate_dsl(dsl)

    issue_for(report, "invalid_approval_node")


def test_approval_approvers_must_be_non_empty_list():
    dsl = six_node_dsl()
    dsl["nodes"][3]["approvers"] = []

    report = validate_dsl(dsl)

    issue_for(report, "invalid_approval_node")


def test_input_map_must_be_dict():
    dsl = six_node_dsl()
    dsl["nodes"][1]["input_map"] = ["$input.question"]

    report = validate_dsl(dsl)

    issue_for(report, "invalid_input_map")


def test_input_map_rejects_container_values():
    dsl = six_node_dsl()
    dsl["nodes"][1]["input_map"] = {"question": {"nested": True}}

    report = validate_dsl(dsl)

    issue_for(report, "invalid_input_map")


def test_input_map_expression_must_compile():
    dsl = six_node_dsl()
    dsl["nodes"][1]["input_map"] = {"question": "$input.question + 1"}

    report = validate_dsl(dsl)

    issue_for(report, "invalid_input_map_expression")


def test_input_map_allows_scalars_and_plain_strings():
    dsl = six_node_dsl()
    dsl["nodes"][1]["input_map"] = {
        "question": "literal",
        "limit": 3,
        "ratio": 0.5,
        "enabled": True,
        "nothing": None,
        "from_ctx": "$input.question",
    }

    assert validate_dsl(dsl).valid is True


# ------------------------------------------------------------- known agent/tool


def test_unknown_agent_rejected_when_known_agents_given():
    report = validate_dsl(six_node_dsl(), known_agents={"planner"})

    issue = issue_for(report, "agent_not_found_or_disabled")
    assert issue.node_id == "report"


def test_known_agents_accepts_all_agent_nodes():
    report = validate_dsl(
        six_node_dsl(), known_agents={"planner", "reporter"}
    )

    assert report.valid is True


def test_unknown_tool_rejected_when_known_tools_given():
    report = validate_dsl(tool_dsl("query_device_status"), known_tools={"other"})

    issue = issue_for(report, "tool_not_found_or_disabled")
    assert issue.node_id == "lookup"


def test_known_tools_accepts_tool_node():
    report = validate_dsl(
        tool_dsl("query_device_status"), known_tools={"query_device_status"}
    )

    assert report.valid is True


def test_known_sets_none_skips_existence_checks():
    assert validate_dsl(six_node_dsl()).valid is True
    assert validate_dsl(tool_dsl()).valid is True


# -------------------------------------------------------------------- 顶层形状


@pytest.mark.parametrize("dsl", [None, [], "nodes", 42])
def test_dsl_must_be_object(dsl):
    report = validate_dsl(dsl)

    issue_for(report, "dsl_not_object")


@pytest.mark.parametrize(
    ("nodes", "edge_code"),
    [
        ([], "nodes_empty"),
        (None, "invalid_nodes"),
        ("nodes", "invalid_nodes"),
    ],
)
def test_nodes_shape(nodes, edge_code):
    report = validate_dsl({"nodes": nodes, "edges": []})

    issue_for(report, edge_code)


def test_edges_must_be_list():
    report = validate_dsl({"nodes": six_node_dsl()["nodes"], "edges": {}})

    issue_for(report, "invalid_edges")


def test_malformed_node_and_edge_entries_are_reported():
    dsl = {"nodes": ["bad", {"type": "agent"}], "edges": ["bad", {"from": "x"}]}
    report = validate_dsl(dsl)

    assert "invalid_node" in codes(report)
    assert "missing_node_id" in codes(report)
    assert "invalid_edge" in codes(report)


# -------------------------------------------------------------------- assert_valid


def test_assert_valid_raises_workflow_validation_error_with_details():
    report = validate_dsl({"nodes": [], "edges": []})

    with pytest.raises(WorkflowValidationError) as exc_info:
        assert_valid(report)

    error = exc_info.value
    assert error.code == 400
    assert error.http_status == 400
    assert isinstance(error.detail["errors"], list)
    assert error.detail["errors"][0]["code"] == "nodes_empty"


def test_assert_valid_does_not_raise_for_valid_report():
    assert_valid(validate_dsl(six_node_dsl()))


# ------------------------------------------------------------------- 表达式：合法


def test_compile_expression_is_cached():
    first = compile_expression("$input.x > 1")
    second = compile_expression("$input.x > 1")

    assert first is second


@pytest.mark.parametrize(
    ("source", "context", "expected"),
    [
        ("$input.x > 1", {"input": {"x": 2}}, True),
        ("$input.x >= 2", {"input": {"x": 2}}, True),
        ("$input.x < 2", {"input": {"x": 2}}, False),
        ("$input.x <= 2", {"input": {"x": 2}}, True),
        ("$input.x == 2", {"input": {"x": 2}}, True),
        ("$input.x != 2", {"input": {"x": 2}}, False),
        ("$input.a.b == 'ok'", {"input": {"a": {"b": "ok"}}}, True),
        ('$input.name == "abc"', {"input": {"name": "abc"}}, True),
        ("$input.x == null", {"input": {"x": None}}, True),
        ("$input.x != null", {"input": {"x": None}}, False),
        ("true", {}, True),
        ("FALSE", {}, False),
        ("Null == null", {}, True),
        ("$input.x > -5", {"input": {"x": -1}}, True),
        ("$input.x < +5", {"input": {"x": 1}}, True),
        ("1 < $input.x < 10", {"input": {"x": 5}}, True),
        ("1 < $input.x < 3", {"input": {"x": 5}}, False),
        ("$input.flag and $input.x > 1", {"input": {"flag": True, "x": 2}}, True),
        ("$input.flag or $input.x > 1", {"input": {"flag": False, "x": 2}}, True),
        ("not $input.flag", {"input": {"flag": False}}, True),
        ("($input.x > 1 and not $input.flag) or $input.y == 3",
         {"input": {"x": 2, "flag": False, "y": 0}}, True),
        (
            "$plan.output.high_risk_count > 0",
            {"plan": {"output": {"high_risk_count": 3}}},
            True,
        ),
        ("$input.text == '设备 在线'", {"input": {"text": "设备 在线"}}, True),
    ],
)
def test_evaluate_expression_valid_sources(source, context, expected):
    assert evaluate_expression(source, context) is expected


def test_evaluate_condition_returns_bool():
    assert evaluate_condition("$input.x > 1", {"input": {"x": 5}}) is True
    assert evaluate_condition("$input.x > 1", {"input": {"x": 0}}) is False


def test_short_circuit_avoids_missing_right_branch():
    assert evaluate_expression(
        "$input.ok or $input.missing.deep.path", {"input": {"ok": True}}
    ) is True
    assert evaluate_expression(
        "$input.ok and $input.missing.deep.path", {"input": {"ok": False}}
    ) is False


# ------------------------------------------------------------------- 表达式：非法


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os')",
        "$input.x.upper()",
        "$input.x + 1",
        "$input.x * 2",
        "$input.x[0]",
        "$input.x = 1",
        "lambda: 1",
        "[x for x in $input.a]",
        "{key: 1 for key in $input.a}",
        "f\"{$input.x}\"",
        "$input.x in $input.y",
        "$input.x is null",
        "$input.__class__",
        "$input._private",
        "$secret.x",
        "foo",
        "$$input.x",
        "$input.x $",
        "$ input.x",
        "",
        "   ",
        "x" * 2001,
        "not " * 120 + "true",
    ],
)
def test_compile_expression_rejects_unsafe_or_invalid_sources(source):
    with pytest.raises(ExpressionError) as exc_info:
        compile_expression(source)

    assert exc_info.value.message


def test_expression_error_is_value_error():
    with pytest.raises(ValueError):
        compile_expression("$input.x + 1")


def test_missing_path_raises_with_path_in_message():
    with pytest.raises(ExpressionError) as exc_info:
        evaluate_expression("$input.question", {})

    assert "$input.question" in exc_info.value.message


def test_missing_node_output_path_raises():
    with pytest.raises(ExpressionError) as exc_info:
        evaluate_expression("$plan.output.count > 0", {"plan": {"output": {}}})

    assert "$plan.output.count" in exc_info.value.message


def test_boolean_operands_must_be_bool():
    with pytest.raises(ExpressionError):
        evaluate_condition("1 and 2", {})

    with pytest.raises(ExpressionError):
        evaluate_expression("$input.x and true", {"input": {"x": 1}})

    with pytest.raises(ExpressionError):
        evaluate_expression("not 1", {})


def test_evaluate_condition_rejects_non_bool_result():
    with pytest.raises(ExpressionError):
        evaluate_condition("$input.x", {"input": {"x": 3}})


def test_equality_rejects_container_values():
    with pytest.raises(ExpressionError):
        evaluate_expression(
            "$input.a == $input.b", {"input": {"a": {}, "b": {}}}
        )

    with pytest.raises(ExpressionError):
        evaluate_expression(
            "$input.a != $input.b", {"input": {"a": [], "b": []}}
        )


def test_ordering_rejects_non_numeric_and_non_string_pairs():
    with pytest.raises(ExpressionError):
        evaluate_expression("$input.a > $input.b", {"input": {"a": {}, "b": {}}})

    with pytest.raises(ExpressionError):
        evaluate_expression("$input.x > 'a'", {"input": {"x": 1}})

    with pytest.raises(ExpressionError):
        evaluate_expression("$input.x < true", {"input": {"x": 1}})
