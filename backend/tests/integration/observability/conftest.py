import pytest
from sqlalchemy import text

from tests.conftest import TENANT

TRACE_ID = "a" * 32
ROOT_ID = "b" * 16
AGENT_ID = "c" * 16


@pytest.fixture
async def seeded_trace(session):
    """直接插表：不经过 tracer，查询 API 的测试不依赖采集管道是否已就绪。"""
    await session.execute(text(
        "INSERT INTO traces (trace_id, tenant_id, name, source, status, total_spans,"
        " start_time) VALUES (:t, :ten, 'demo', 'test', 'succeeded', 2, now())"),
        {"t": TRACE_ID, "ten": TENANT})
    for span_id, parent, name, span_type in [
        (ROOT_ID, None, "wf", "workflow"),
        (AGENT_ID, ROOT_ID, "agent.device", "agent"),
    ]:
        await session.execute(text(
            "INSERT INTO spans (span_id, tenant_id, trace_id, parent_span_id, name,"
            " span_type, status, start_time)"
            " VALUES (:s, :ten, :t, :p, :n, :ty, 'succeeded', now())"),
            {"s": span_id, "ten": TENANT, "t": TRACE_ID, "p": parent,
             "n": name, "ty": span_type})
    return TRACE_ID
