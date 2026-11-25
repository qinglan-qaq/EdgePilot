import pytest

from app.tools.registry import ToolRegistry, ToolResult
from app.tools.executor import ToolExecutionError


@pytest.mark.asyncio
async def test_schema_validation_returns_422_code():
    reg = ToolRegistry()
    reg.register("echo", "1.0.0",
                 input_schema={"type": "object", "required": ["msg"],
                               "properties": {"msg": {"type": "string"}}},
                 output_schema=None, handler=lambda args: {"ok": args["msg"]})
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("echo", {}, ctx=None)
    assert ei.value.code == "input_schema_invalid"


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout_code():
    import asyncio
    reg = ToolRegistry()
    async def slow(args): await asyncio.sleep(1)
    reg.register("slow", "1.0.0", input_schema=None, output_schema=None,
                 handler=slow, timeout_ms=10, max_retries=0)
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("slow", {}, ctx=None)
    assert ei.value.code == "timeout"


@pytest.mark.asyncio
async def test_retry_records_attempts():
    reg = ToolRegistry()
    calls = {"n": 0}
    async def flaky(args):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("boom")
        return {"ok": True}
    reg.register("flaky", "1.0.0", input_schema=None, output_schema=None,
                 handler=flaky, max_retries=2)
    result = await reg.execute("flaky", {}, ctx=None)
    assert result.ok is True
    assert calls["n"] == 2
