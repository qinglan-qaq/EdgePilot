import pytest

from app.tools.registry import ToolContext, ToolRegistry
from app.tools.executor import ToolExecutionError


async def test_agent_bound_allowlist_enforced():
    reg = ToolRegistry()
    reg.register("danger", "1.0.0", input_schema=None, output_schema=None,
                 handler=lambda args: {"ok": True}, side_effect=True)
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("danger", {}, ctx=ToolContext(agent_tool_ids=set()))
    assert ei.value.code == "permission_denied"
