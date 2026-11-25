"""Agent 类型注册表：把 ``agent_type`` 映射到输出模型与消息构造器。

未知类型统一回退到 Supervisor 契约（全部字段可选、无证据强制），
保证新建类型即便尚未实现专属模块也能通过试跑而不是 500。
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable

from app.agents import device, diagnosis, operations, report, supervisor, vision
from app.agents.schemas import output_model_for

AGENT_MODULES: dict[str, ModuleType] = {
    "supervisor": supervisor,
    "device": device,
    "vision": vision,
    "diagnosis": diagnosis,
    "operations": operations,
    "report": report,
}

MessageBuilder = Callable[[Any, dict], list[dict]]


def module_for(agent_type: str) -> ModuleType:
    """返回角色模块；未知类型回退 Supervisor 模块。"""
    return AGENT_MODULES.get(agent_type, supervisor)


def get_output_model(agent_type: str):
    """返回角色输出模型；未知类型回退 SupervisorOutput。"""
    return output_model_for(agent_type)


def get_build_messages(agent_type: str) -> MessageBuilder:
    """返回角色消息构造器；未知类型使用通用（Supervisor）构造器。"""
    return module_for(agent_type).build_messages
