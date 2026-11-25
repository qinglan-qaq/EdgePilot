"""Prompt 加载与消息构造。

默认 prompt 以 Markdown 文件维护在 ``backend/agents/prompts/{agent_type}.md``
（设计 §8.1 的六个角色各一份）。优先级：

1. ``agent.config["system_prompt"]``（管理员在 Agent 配置里覆盖）；
2. 对应 ``{agent_type}.md`` 的默认内容；
3. 最后的兜底模板 ``你是 {agent_type} Agent。``。

文件缺失时返回空串而**不抛异常**，避免一个没写 prompt 的角色阻断试跑。
user message 固定为任务参数的规范 JSON（``ensure_ascii=False``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).resolve().parents[2] / "agents" / "prompts"

_GENERIC_PROMPT = "你是 {agent_type} Agent。"


def prompt_path(agent_type: str) -> Path:
    """返回某个角色的默认 prompt 文件路径（不保证存在）。"""
    return PROMPT_DIR / f"{agent_type}.md"


def load_default_prompt(agent_type: str) -> str:
    """读取 ``{agent_type}.md``；文件缺失或不可读时返回空串。"""
    try:
        return prompt_path(agent_type).read_text(encoding="utf-8")
    except OSError:
        return ""


def build_messages(agent: Any, task: dict) -> list[dict]:
    """按角色构造 ``messages``：system prompt + 任务 JSON。

    Args:
        agent: 具备 ``agent_type`` 与 ``config`` 属性的对象（ORM 行或测试替身）。
        task: 任务参数，原样序列化为 user message。

    Returns:
        OpenAI 兼容的 ``messages`` 列表，至少包含 system 与 user 两条。
    """
    config = getattr(agent, "config", None)
    config = config if isinstance(config, dict) else {}
    agent_type = str(getattr(agent, "agent_type", "") or "").strip() or "generic"

    system_prompt = config.get("system_prompt")
    if not (isinstance(system_prompt, str) and system_prompt.strip()):
        system_prompt = load_default_prompt(agent_type)
    if not system_prompt.strip():
        system_prompt = _GENERIC_PROMPT.format(agent_type=agent_type)

    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": json.dumps(task, ensure_ascii=False, default=str),
        },
    ]
