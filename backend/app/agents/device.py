"""Device Agent 契约：设备/节点状态输出模型 + 默认 prompt。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.agents.prompts import build_messages as _build_messages, prompt_path
from app.agents.schemas import DeviceOutput

AGENT_TYPE = "device"
OUTPUT_MODEL = DeviceOutput
SYSTEM_PROMPT_PATH: Path = prompt_path(AGENT_TYPE)


def build_messages(agent: Any, task: dict) -> list[dict]:
    """构造 Device 的对话消息（system prompt 优先取 Agent 配置）。"""
    return _build_messages(agent, task)
