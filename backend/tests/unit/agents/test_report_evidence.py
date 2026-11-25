"""报告生成与证据链回溯测试（Task 16）。

全部用例只走 ``app.agents.report`` 的确定性构建器：不调用 LLM、不碰数据库、
不触网，运行时间接近 0。核心断言是「报告里出现的每个 ``[n]`` 都能在
``evidence[n-1]`` 找到对应项」。
"""

from __future__ import annotations

import re

import pytest

from app.agents.report import (
    EVIDENCE_TYPES,
    ReportEvidenceError,
    ReportFinding,
    build_report,
    collect_findings,
    render_report,
)
from app.agents.schemas import Evidence, ReportOutput


def _result(
    agent_type: str,
    output: object,
    *,
    status: str = "succeeded",
    goal: str = "子任务",
) -> dict:
    """构造一条与 Supervisor 结果条目同构的 dict。"""
    return {
        "agent_type": agent_type,
        "goal": goal,
        "input": {},
        "status": status,
        "output": output,
        "error": None if status == "succeeded" else "failed",
        "agent_run_id": 1,
        "total_tokens": 10,
    }


def _citations(markdown: str) -> list[int]:
    return [int(number) for number in re.findall(r"\[(\d+)\]", markdown)]


def test_two_results_citations_all_map_to_evidence():
    """成功结果 → 每个引用编号都在 evidence 范围内且有对应条目。"""
    results = [
        _result(
            "device",
            {
                "answer": "设备在线",
                "evidence": [
                    {"type": "metric", "ref": "m1", "snippet": "cpu 95%"}
                ],
            },
        ),
        _result(
            "operations",
            {
                "summary": "节点离线",
                "evidence": [{"type": "event_id", "ref": "evt-1"}],
            },
        ),
    ]

    report = build_report(results)
    citations = _citations(report["markdown"])

    assert citations, "报告必须给出引用编号"
    assert all(1 <= number <= len(report["evidence"]) for number in citations)
    for number in set(citations):
        assert report["evidence"][number - 1]["ref"]

    assert [item["type"] for item in report["evidence"]] == ["metric", "event_id"]
    assert all(item["type"] in EVIDENCE_TYPES for item in report["evidence"])
    assert all(highlight["evidence"] for highlight in report["highlights"])
    assert [highlight["statement"] for highlight in report["highlights"]] == [
        "设备在线",
        "节点离线",
    ]
    assert "1. [1] 设备在线" in report["markdown"]
    assert "2. [2] 节点离线" in report["markdown"]


def test_conclusion_without_evidence_is_omitted_but_evidenced_one_kept():
    """无证据的非空结论被跳过；有证据的结论照常写入。"""
    results = [
        _result("device", {"answer": "无证据结论", "evidence": []}),
        _result(
            "operations",
            {
                "summary": "有证据结论",
                "evidence": [{"type": "metric", "ref": "m2"}],
            },
        ),
    ]

    report = build_report(results)

    assert [highlight["statement"] for highlight in report["highlights"]] == [
        "有证据结论"
    ]
    assert "无证据结论" not in report["markdown"]
    assert len(report["evidence"]) == 1


def test_all_results_lack_evidence_yields_honest_insufficient_report():
    """全部结论无证据时输出「证据不足」，不编造任何证据或结论。"""
    results = [
        _result("device", {"answer": "设备在线", "evidence": []}),
        _result("operations", {"summary": "节点离线", "evidence": []}),
    ]

    report = build_report(results)

    assert report["evidence"] == []
    assert report["highlights"] == []
    assert report["finding_count"] == 0
    assert "证据不足：没有可回溯证据支持的结论。" in report["markdown"]
    ReportOutput.model_validate(report)


def test_invalid_evidence_type_fails_closed():
    """证据类型不在白名单时抛 ReportEvidenceError，不返回报告。"""
    results = [
        _result(
            "device",
            {
                "answer": "设备在线",
                "evidence": [{"type": "span_id", "ref": "span-1"}],
            },
        )
    ]

    with pytest.raises(ReportEvidenceError) as excinfo:
        build_report(results)

    assert excinfo.value.code == "report_evidence_invalid"
    assert str(excinfo.value).startswith("report_evidence_invalid:")


@pytest.mark.parametrize(
    "item",
    [
        {"type": "metric", "ref": "   "},
        {"type": "metric"},
        {"type": 1, "ref": "m1"},
        {"type": "metric", "ref": 1},
        {"type": "metric", "ref": "m1", "snippet": 5},
        "metric:m1",
    ],
)
def test_invalid_evidence_items_fail_closed(item):
    """ref 空白、缺字段、类型错误、snippet 非字符串等都拒绝。"""
    results = [_result("device", {"answer": "设备在线", "evidence": [item]})]

    with pytest.raises(ReportEvidenceError) as excinfo:
        build_report(results)

    assert excinfo.value.code == "report_evidence_invalid"


def test_duplicate_evidence_is_deduped_and_shares_citation_number():
    """两条结论复用同一 (type, ref) 时只保留一条证据，并共用 [1]。"""
    results = [
        _result(
            "device",
            {"answer": "结论甲", "evidence": [{"type": "metric", "ref": "m1"}]},
        ),
        _result(
            "operations",
            {"summary": "结论乙", "evidence": [{"type": "metric", "ref": "m1"}]},
        ),
    ]

    report = build_report(results)

    assert len(report["evidence"]) == 1
    assert report["evidence"][0]["ref"] == "m1"
    assert "1. [1] 结论甲" in report["markdown"]
    assert "2. [1] 结论乙" in report["markdown"]
    assert "- [1] `metric:m1`" in report["markdown"]
    assert all(
        highlight["evidence"][0]["ref"] == "m1"
        for highlight in report["highlights"]
    )
    ReportOutput.model_validate(report)


def test_report_output_validates_and_failed_entries_are_ignored():
    """返回结构能通过 ReportOutput 校验；失败/无 output 条目一律忽略。"""
    results = [
        _result(
            "device",
            {"answer": "在线", "evidence": [{"type": "event_id", "ref": "e1"}]},
        ),
        _result(
            "vision",
            {"summary": "不应出现", "evidence": [{"type": "metric", "ref": "m9"}]},
            status="failed",
        ),
        {"agent_type": "operations", "status": "succeeded", "output": None},
    ]

    report = build_report(results)
    parsed = ReportOutput.model_validate(report)

    assert parsed.title == "EdgePilot 诊断报告"
    assert parsed.markdown == report["markdown"]
    assert [highlight.statement for highlight in parsed.highlights] == ["在线"]
    assert [item.ref for item in parsed.evidence] == ["e1"]
    assert "不应出现" not in report["markdown"]


def test_build_report_with_empty_results_is_honest():
    report = build_report([])

    assert report["title"] == "EdgePilot 诊断报告"
    assert report["evidence"] == []
    assert report["highlights"] == []
    assert report["finding_count"] == 0
    assert report["markdown"] == (
        "# EdgePilot 诊断报告\n\n证据不足：没有可回溯证据支持的结论。\n"
    )
    ReportOutput.model_validate(report)


def test_title_precedence():
    """显式 title → task.title → task.goal → 默认值。"""
    assert build_report([], title="显式标题")["title"] == "显式标题"
    assert (
        build_report([], task={"title": "任务标题", "goal": "目标"})["title"]
        == "任务标题"
    )
    assert build_report([], task={"goal": "目标"})["title"] == "目标"
    assert build_report([], task={})["title"] == "EdgePilot 诊断报告"


def test_collect_findings_derives_statement_per_agent_type():
    """device 回退 device_status；vision/operations/diagnosis 各取规定字段。"""
    results = [
        _result(
            "device",
            {
                "answer": "   ",
                "device_status": "offline",
                "evidence": [{"type": "event_id", "ref": "e1"}],
            },
        ),
        _result(
            "vision",
            {"summary": "画面异常", "evidence": [{"type": "chunk_id", "ref": "c1"}]},
        ),
        _result(
            "diagnosis",
            {"conclusion": "设备过热", "evidence": [{"type": "metric", "ref": "m1"}]},
        ),
        _result(
            "report",
            {"markdown": "# x", "evidence": [{"type": "metric", "ref": "m2"}]},
        ),
        _result(
            "supervisor",
            {"summary": "编排完成", "evidence": [{"type": "metric", "ref": "m3"}]},
        ),
    ]

    findings = collect_findings(results)

    assert [(item.agent_type, item.statement) for item in findings] == [
        ("device", "offline"),
        ("vision", "画面异常"),
        ("diagnosis", "设备过热"),
    ]
    assert all(isinstance(item.evidence[0], Evidence) for item in findings)


def test_render_report_citation_format_and_shared_number():
    """Markdown 形态固定：结论行自带 [n]，证据列表按首用顺序编号。"""
    first = ReportFinding(
        statement="甲",
        agent_type="device",
        evidence=(Evidence(type="metric", ref="m1", snippet="cpu 95%"),),
    )
    second = ReportFinding(
        statement="乙",
        agent_type="operations",
        evidence=(
            Evidence(type="metric", ref="m1", snippet="cpu 95%"),
            Evidence(type="event_id", ref="evt-1"),
        ),
    )

    rendered = render_report("T", [first, second])

    assert rendered["markdown"] == (
        "# T\n"
        "\n"
        "## 结论\n"
        "\n"
        "1. [1] 甲\n"
        "2. [1][2] 乙\n"
        "\n"
        "## 证据\n"
        "\n"
        "- [1] `metric:m1` — cpu 95%\n"
        "- [2] `event_id:evt-1`\n"
    )
    assert rendered["evidence"] == [
        {"type": "metric", "ref": "m1", "snippet": "cpu 95%"},
        {"type": "event_id", "ref": "evt-1", "snippet": None},
    ]
    assert rendered["finding_count"] == 2
    assert _citations(rendered["markdown"]) == [1, 1, 2, 1, 2]
