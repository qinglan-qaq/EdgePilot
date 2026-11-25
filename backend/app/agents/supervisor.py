"""Supervisor Agent 契约：编排输出模型 + 默认 prompt。

本期只固定 I/O 契约；Plan-and-Execute 图结构与预算控制由 Task 14 实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.prompts import build_messages as _build_messages, prompt_path
from app.agents.schemas import SupervisorOutput

AGENT_TYPE = "supervisor"
OUTPUT_MODEL = SupervisorOutput
SYSTEM_PROMPT_PATH: Path = prompt_path(AGENT_TYPE)


def build_messages(agent: Any, task: dict) -> list[dict]:
    """构造 Supervisor 的对话消息（system prompt 优先取 Agent 配置）。"""
    return _build_messages(agent, task)
