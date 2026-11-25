"""Report Agent 契约：Markdown 报告输出模型 + 默认 prompt。

证据链回溯（结论编号必须命中 ``evidence``）由 Task 16 的报告生成器实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.prompts import build_messages as _build_messages, prompt_path
from app.agents.schemas import ReportOutput

AGENT_TYPE = "report"
OUTPUT_MODEL = ReportOutput
SYSTEM_PROMPT_PATH: Path = prompt_path(AGENT_TYPE)


def build_messages(agent: Any, task: dict) -> list[dict]:
    """构造 Report 的对话消息（system prompt 优先取 Agent 配置）。"""
    return _build_messages(agent, task)
