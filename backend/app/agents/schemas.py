"""六类 Agent 的结构化输出模型与证据校验。

核心约束（MVP 设计 §8.1，写进代码而不只是 prompt）：

* 任何输出都必须通过 Pydantic 校验，校验失败由调用方重试 1 次；
* 带事实结论的 Agent（device / vision / operations / diagnosis）在
  ``evidence == []`` 时**不允许**给出非空结论，必须把结论字段置空；
* Supervisor 只做编排、Report 只汇总既有结果，二者不在此处强制证据；
  Report 的证据链回溯由 Task 16 的 ``test_report_evidence.py`` 覆盖。

``extra="allow"`` 保留 LLM 额外返回的字段（例如 device 的 ``device``、
vision 的 ``conclusion``），避免因多返回一个键就判校验失败。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Evidence(BaseModel):
    """一条可回溯证据：类型 + 引用，``snippet`` 为原文片段（可为空）。"""

    model_config = ConfigDict(extra="allow")

    type: str
    ref: str
    snippet: str | None = None


def _is_non_empty(value: Any) -> bool:
    """判断结论字段是否真的给出了内容；空白字符串按空处理。"""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


class EvidenceBackedOutput(BaseModel):
    """所有领域输出的基类：证据列表 + 「无证据不得下结论」校验。

    子类通过 ``CONCLUSION_FIELDS`` 声明哪些字段属于「事实结论」；只有这些
    字段非空且 ``evidence`` 为空时才拒绝，空结论 + 空证据是合法的
    「证据不足、不编造」路径。
    """

    model_config = ConfigDict(extra="allow")

    evidence: list[Evidence] = Field(default_factory=list)
    CONCLUSION_FIELDS: ClassVar[tuple[str, ...]] = ()

    @model_validator(mode="after")
    def _require_evidence_for_conclusions(self) -> "EvidenceBackedOutput":
        """结论字段非空时必须至少有一条 evidence。"""
        if self.evidence:
            return self
        for field in self.CONCLUSION_FIELDS:
            if _is_non_empty(getattr(self, field, None)):
                raise ValueError(
                    f"字段 {field} 给出了结论，但 evidence 为空；"
                    "无证据的结论必须置空（不得编造证据）"
                )
        return self


class SupervisorOutput(EvidenceBackedOutput):
    """Supervisor 的编排输出：只拆解/派发/汇总，不直接断言事实。"""

    plan: list[str] = Field(default_factory=list)
    assignments: list[dict] = Field(default_factory=list)
    summary: str | None = None


class DeviceOutput(EvidenceBackedOutput):
    """设备 Agent 输出：在线状态/回答必须由证据支撑。"""

    answer: str | None
    device_status: str | None = None
    CONCLUSION_FIELDS: ClassVar[tuple[str, ...]] = ("answer", "device_status")


class VisionOutput(EvidenceBackedOutput):
    """视觉 Agent 输出：事件摘要属于结论，必须带证据。"""

    summary: str | None
    events: list[dict] = Field(default_factory=list)
    CONCLUSION_FIELDS: ClassVar[tuple[str, ...]] = ("summary",)


class OperationsOutput(EvidenceBackedOutput):
    """运维 Agent 输出：资源/服务结论必须带指标证据。"""

    summary: str | None
    findings: list[dict] = Field(default_factory=list)
    CONCLUSION_FIELDS: ClassVar[tuple[str, ...]] = ("summary",)


class DiagnosisOutput(EvidenceBackedOutput):
    """诊断 Agent 输出：缺证据的诊断结论直接判校验失败。"""

    conclusion: str | None
    possible_causes: list[str] = Field(default_factory=list)
    CONCLUSION_FIELDS: ClassVar[tuple[str, ...]] = ("conclusion",)


class ReportHighlight(BaseModel):
    """报告中一条可回溯结论：statement 必须至少有一条 evidence。"""

    model_config = ConfigDict(extra="allow")

    statement: str
    evidence: list[Evidence] = Field(min_length=1)


class ReportOutput(EvidenceBackedOutput):
    """报告 Agent 输出：Markdown 主体 + 标题 + 可回溯结论列表。

    本期不在此处强制 ``CONCLUSION_FIELDS``（``ReportOutput(markdown=...)``
    仍必须合法）；证据链由 ``app.agents.report`` 的确定性构建器负责，
    构建器返回的结构可直接通过本模型校验。
    """

    title: str | None = None
    markdown: str
    highlights: list[ReportHighlight] = Field(default_factory=list)


OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "supervisor": SupervisorOutput,
    "device": DeviceOutput,
    "vision": VisionOutput,
    "diagnosis": DiagnosisOutput,
    "operations": OperationsOutput,
    "report": ReportOutput,
}

# 兼容旧名字：Task 12 内部使用过 ``AgentOutput`` / ``AgentEvidence``。
AgentEvidence = Evidence
AgentOutput = DeviceOutput


def output_model_for(agent_type: str) -> type[BaseModel]:
    """返回 ``agent_type`` 对应的输出模型；未知类型回退 Supervisor（无必填）。"""
    return OUTPUT_MODELS.get(agent_type, SupervisorOutput)


def output_schema_for(agent_type: str) -> dict[str, Any]:
    """返回传给 LLM 的 JSON Schema（Pydantic 生成，覆盖同模型的字段约束）。"""
    return output_model_for(agent_type).model_json_schema()


def parse_output(agent_type: str, candidate: dict) -> dict:
    """按 ``agent_type`` 校验候选输出并转成纯 dict。

    Raises:
        pydantic.ValidationError: 字段类型或证据规则不满足时抛出，由调用方
            决定重试或落 ``agent_output_invalid``。
    """
    model = output_model_for(agent_type)
    return model.model_validate(candidate).model_dump()
