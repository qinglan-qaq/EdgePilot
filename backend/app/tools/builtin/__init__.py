"""9 个内置工具的注册入口（MVP 设计 §8.2）。

其中 7 个为本地实现；``query_device_status`` 与 ``search_knowledge_base``
后续经 MCP 暴露（Task 12），本地 handler 保持同一套权限/超时/审计外壳。
"""

from app.tools.builtin import device, knowledge, operations, report, vision
from app.tools.registry import ToolRegistry, get_default_registry

# MVP §8.2 约定的 9 个工具名（注册后应恰好一一对应）
BUILTIN_TOOL_IDS: tuple[str, ...] = (
    "query_device_status",
    "query_device_telemetry",
    "query_edge_node_metrics",
    "query_vision_events",
    "query_alarm_events",
    "query_model_status",
    "query_task_history",
    "search_knowledge_base",
    "generate_report",
)

_REGISTRARS = (
    device.register,
    vision.register,
    operations.register,
    knowledge.register,
    report.register,
)


def register_builtins(registry: ToolRegistry) -> ToolRegistry:
    """把全部内置工具注册进给定注册表；重复调用会覆盖同名工具。"""
    for registrar in _REGISTRARS:
        registrar(registry)
    return registry


__all__ = [
    "BUILTIN_TOOL_IDS",
    "get_default_registry",
    "register_builtins",
]
