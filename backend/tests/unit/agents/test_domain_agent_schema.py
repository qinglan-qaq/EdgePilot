"""六类 Agent 输出模型的证据约束契约测试（Task 13）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.schemas import (
    OUTPUT_MODELS,
    DiagnosisOutput,
    DeviceOutput,
    OperationsOutput,
    ReportOutput,
    SupervisorOutput,
    VisionOutput,
    output_model_for,
    output_schema_for,
    parse_output,
)

_EVIDENCE = [{"type": "metric", "ref": "m1", "snippet": "cpu 95%"}]


def test_diagnosis_conclusion_without_evidence_is_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        DiagnosisOutput(conclusion="过热", evidence=[])


def test_diagnosis_conclusion_with_evidence_is_valid():
    out = DiagnosisOutput(conclusion="过热", evidence=_EVIDENCE)
    assert out.evidence[0].ref == "m1"


def test_diagnosis_without_evidence_must_null_conclusion():
    """“不编造”路径：结论置空 + 空证据是合法的。"""
    out = DiagnosisOutput(conclusion=None, evidence=[])
    assert out.conclusion is None
    assert out.evidence == []


def test_device_answer_without_evidence_is_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        DeviceOutput(answer="在线")


def test_device_all_empty_is_valid():
    out = DeviceOutput(answer=None, device_status=None, evidence=[])
    assert out.answer is None and out.device_status is None


def test_device_with_evidence_is_valid():
    out = DeviceOutput(answer="在线", evidence=_EVIDENCE)
    assert out.answer == "在线"


def test_vision_summary_without_evidence_is_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        VisionOutput(summary="异常")


def test_operations_summary_without_evidence_is_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        OperationsOutput(summary="节点离线")


def test_report_and_supervisor_do_not_enforce_evidence():
    """Report 的证据链留给 Task 16；Supervisor 只编排不断言事实。"""
    assert ReportOutput(markdown="# 报告").markdown == "# 报告"
    assert SupervisorOutput(plan=["a"]).plan == ["a"]


def test_parse_output_rejects_diagnosis_without_evidence():
    with pytest.raises(ValidationError, match="evidence"):
        parse_output("diagnosis", {"conclusion": "设备过热", "evidence": []})


def test_parse_output_dumps_valid_diagnosis():
    parsed = parse_output(
        "diagnosis", {"conclusion": "设备过热", "evidence": _EVIDENCE}
    )
    assert parsed["conclusion"] == "设备过热"
    assert parsed["evidence"][0]["snippet"] == "cpu 95%"


def test_diagnosis_schema_reflects_contract():
    schema = output_schema_for("diagnosis")
    assert schema["type"] == "object"
    assert {"conclusion", "evidence"} <= set(schema["properties"])
    assert "conclusion" in schema.get("required", [])


def test_unknown_agent_type_falls_back_to_supervisor_contract():
    assert set(OUTPUT_MODELS) == {
        "supervisor", "device", "vision", "diagnosis", "operations", "report"
    }
    assert output_model_for("unknown-role") is SupervisorOutput
    assert output_schema_for("unknown-role")["type"] == "object"
