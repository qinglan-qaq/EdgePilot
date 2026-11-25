"""Report Agent 契约 + 确定性报告构建器（证据链回溯）。

* ``collect_findings`` 只从 Supervisor 的**成功**子结果里提取结论，
  跳过 ``report`` / ``supervisor``，绝不调用 LLM、绝不编造内容；
* ``render_report`` 按证据首次使用顺序全局编号，``(type, ref)`` 相同即
  同号，报告里的 ``[n]`` 永远对应返回的 ``evidence[n-1]``；
* 证据类型严格限定 ``event_id`` / ``metric`` / ``chunk_id``，非法证据
  直接 ``ReportEvidenceError``（fail closed，不静默丢弃）；
* 结论非空但无证据时不写入报告，最终无结论则诚实输出「证据不足」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.prompts import build_messages as _build_messages, prompt_path
from app.agents.schemas import Evidence, ReportOutput

AGENT_TYPE = "report"
OUTPUT_MODEL = ReportOutput
SYSTEM_PROMPT_PATH: Path = prompt_path(AGENT_TYPE)

DEFAULT_TITLE = "EdgePilot 诊断报告"
INSUFFICIENT_EVIDENCE = "证据不足：没有可回溯证据支持的结论。"

#: 允许写进报告的证据类型（MVP 设计 §8.1）。
EVIDENCE_TYPES: frozenset[str] = frozenset({"event_id", "metric", "chunk_id"})

#: 各领域 Agent 的结论字段（按优先级）。report/supervisor 不参与汇总。
_STATEMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "device": ("answer", "device_status"),
    "vision": ("summary",),
    "operations": ("summary",),
    "diagnosis": ("conclusion",),
}

_SKIPPED_AGENT_TYPES = frozenset({"report", "supervisor"})


class ReportEvidenceError(ValueError):
    """证据链不合法；``code`` 固定为 ``report_evidence_invalid``。"""

    code = "report_evidence_invalid"


@dataclass(frozen=True)
class ReportFinding:
    """一条结论及其（非空）证据；evidence 已通过严格校验。"""

    statement: str
    agent_type: str
    evidence: tuple[Evidence, ...]


def build_messages(agent: Any, task: dict) -> list[dict]:
    """构造 Report 的对话消息（system prompt 优先取 Agent 配置）。"""
    return _build_messages(agent, task)


# ------------------------------------------------------------------ 结论提取


def collect_findings(results: Sequence[Any]) -> list[ReportFinding]:
    """从 Supervisor 子结果中提取可回溯结论（不调用 LLM，不补造证据）。

    只处理 ``status == "succeeded"`` 且 ``output`` 为字典的条目；每类 Agent
    取一个结论字段，结论为空/空白、或证据为空时不生成 finding。证据项非法
    （类型不在白名单、``ref`` 为空等）会抛 ``ReportEvidenceError``。
    """
    findings: list[ReportFinding] = []
    for entry in results:
        if _field(entry, "status") != "succeeded":
            continue
        output = _field(entry, "output")
        if not isinstance(output, Mapping):
            continue

        agent_type = _text(_field(entry, "agent_type"))
        if agent_type is None:
            continue
        agent_type = agent_type.strip().lower()
        if agent_type in _SKIPPED_AGENT_TYPES:
            continue
        statement_fields = _STATEMENT_FIELDS.get(agent_type)
        if not statement_fields:
            continue

        # 先严格校验证据（fail closed），再判断是否有结论可写。
        evidence = tuple(
            _coerce_evidence(item) for item in _raw_evidence(output)
        )
        statement = _first_statement(output, statement_fields)
        if statement is None:
            continue
        if not evidence:
            # 非空结论 + 空证据：不写进报告（由 Markdown 说明证据不足），
            # 但绝不替它补造证据。
            continue
        findings.append(
            ReportFinding(
                statement=statement,
                agent_type=agent_type,
                evidence=evidence,
            )
        )
    return findings


def _first_statement(output: Mapping, fields: Sequence[str]) -> str | None:
    """按优先级取第一个非空白结论字段，原样保留字符串。"""
    for field in fields:
        statement = _text(output.get(field))
        if statement is not None and statement.strip():
            return statement
    return None


def _raw_evidence(output: Mapping) -> Sequence[Any]:
    raw = output.get("evidence")
    if isinstance(raw, (list, tuple)):
        return raw
    return ()


def _coerce_evidence(item: Any) -> Evidence:
    """严格校验并归一化一条证据；不合法直接 fail closed。"""
    if isinstance(item, Evidence):
        evidence_type = item.type
        ref = item.ref
        snippet = item.snippet
    elif isinstance(item, Mapping):
        if "type" not in item or "ref" not in item:
            raise _evidence_error("evidence 项缺少 type 或 ref 字段")
        evidence_type = item.get("type")
        ref = item.get("ref")
        snippet = item.get("snippet")
    else:
        raise _evidence_error(
            f"evidence 项必须是对象或 Evidence，实际为 {type(item).__name__}"
        )

    if not isinstance(evidence_type, str):
        raise _evidence_error("evidence.type 必须是字符串")
    evidence_type = evidence_type.strip()
    if evidence_type not in EVIDENCE_TYPES:
        allowed = "/".join(sorted(EVIDENCE_TYPES))
        raise _evidence_error(
            f"evidence.type 必须是 {allowed} 之一，实际为 {evidence_type!r}"
        )

    if not isinstance(ref, str):
        raise _evidence_error("evidence.ref 必须是非空字符串")
    ref = ref.strip()
    if not ref:
        raise _evidence_error("evidence.ref 必须是非空字符串")

    if snippet is not None and not isinstance(snippet, str):
        raise _evidence_error("evidence.snippet 必须是字符串或 null")

    return Evidence(type=evidence_type, ref=ref, snippet=snippet)


def _evidence_error(detail: str) -> ReportEvidenceError:
    return ReportEvidenceError(f"{ReportEvidenceError.code}: {detail}")


# ------------------------------------------------------------------ 报告渲染


def render_report(title: str, findings: Sequence[ReportFinding]) -> dict:
    """渲染 Markdown 并按首次使用顺序全局编号证据（相同证据同号）。

    返回结构与 ``ReportOutput`` 对齐：``title`` / ``markdown`` / ``evidence``
    / ``highlights`` / ``finding_count``。无任何有效结论时输出「证据不足」，
    ``evidence`` 与 ``highlights`` 均为空列表。
    """
    numbered: dict[tuple[str, str], int] = {}
    evidence: list[Evidence] = []
    rendered: list[tuple[ReportFinding, list[int], list[Evidence]]] = []

    for finding in findings:
        statement = finding.statement
        if not isinstance(statement, str) or not statement.strip():
            continue

        citations: list[int] = []
        finding_evidence: list[Evidence] = []
        local_keys: set[tuple[str, str]] = set()
        for raw in finding.evidence:
            item = _coerce_evidence(raw)
            key = (item.type, item.ref)
            number = numbered.get(key)
            if number is None:
                evidence.append(item)
                number = len(evidence)
                numbered[key] = number
            if key not in local_keys:
                local_keys.add(key)
                finding_evidence.append(item)
            if number not in citations:
                citations.append(number)

        if not citations:
            continue
        rendered.append((finding, citations, finding_evidence))

    markdown = _render_markdown(title, rendered, evidence)
    highlights = [
        {
            "statement": finding.statement,
            "evidence": [item.model_dump() for item in finding_evidence],
        }
        for finding, _citations, finding_evidence in rendered
    ]
    return {
        "title": title,
        "markdown": markdown,
        "evidence": [item.model_dump() for item in evidence],
        "highlights": highlights,
        "finding_count": len(rendered),
    }


def _render_markdown(
    title: str,
    rendered: Sequence[tuple[ReportFinding, list[int], list[Evidence]]],
    evidence: Sequence[Evidence],
) -> str:
    lines = [f"# {title}", ""]
    if not rendered:
        lines.append(INSUFFICIENT_EVIDENCE)
        return "\n".join(lines) + "\n"

    lines.extend(["## 结论", ""])
    for index, (finding, citations, _item) in enumerate(rendered, start=1):
        tags = "".join(f"[{number}]" for number in citations)
        lines.append(f"{index}. {tags} {finding.statement}")

    lines.extend(["", "## 证据", ""])
    for number, item in enumerate(evidence, start=1):
        reference = f"`{item.type}:{item.ref}`"
        if item.snippet:
            lines.append(f"- [{number}] {reference} — {item.snippet}")
        else:
            lines.append(f"- [{number}] {reference}")
    return "\n".join(lines) + "\n"


def build_report(
    results: Sequence[Any],
    *,
    title: str | None = None,
    task: Mapping[str, Any] | None = None,
) -> dict:
    """从 Supervisor 子结果构建报告；标题优先级：显式 → task.title → task.goal → 默认。"""
    findings = collect_findings(results)
    return render_report(_resolve_title(title, task), findings)


def _resolve_title(
    title: str | None, task: Mapping[str, Any] | None
) -> str:
    candidates: list[Any] = [title]
    if isinstance(task, Mapping):
        candidates.extend([task.get("title"), task.get("goal")])
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return DEFAULT_TITLE


# ---------------------------------------------------------------------- 工具


def _field(value: Any, name: str) -> Any:
    """兼容 dict 与对象形式的 Supervisor 结果条目。"""
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _text(value: Any) -> str | None:
    """只接受非空字符串；其它类型视为未给出结论。"""
    return value if isinstance(value, str) else None
