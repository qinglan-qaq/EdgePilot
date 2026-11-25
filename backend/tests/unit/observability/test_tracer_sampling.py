import asyncio
import uuid

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.collector import EventCollector
from app.observability.tracer import Tracer


def test_sample_rate_zero_skips_everything():
    collector = EventCollector(maxsize=10, batch_size=100, flush_ms=1000, writer=None)
    t = Tracer(enabled=True, sample_rate=0.0, collector=collector)

    async def scenario():
        async with t.start_trace("demo") as trace:
            async with t.start_span("work", "agent"):
                pass
        return trace.trace_id

    token = set_tenant_id(uuid.uuid4())
    try:
        assert asyncio.run(scenario()) == ""
    finally:
        reset_tenant_id(token)
    assert collector.pending() == []
