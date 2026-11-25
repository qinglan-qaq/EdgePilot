from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.sweeper import sweep_orphans


@pytest.mark.asyncio
async def test_sweep_marks_stale_running_rows_timeout(session):
    import uuid

    tid = uuid.uuid4()
    token = set_tenant_id(tid)
    try:
        old = datetime.now(timezone.utc) - timedelta(minutes=30)
        await session.execute(text("""
            INSERT INTO traces (tenant_id, trace_id, name, source, status, start_time)
            VALUES (:t, 'deadbeef', 'x', 'workflow_run', 'running', :s)
        """), {"t": tid, "s": old})
        await session.commit()

        assert await sweep_orphans(session, timeout_minutes=10) == 1
        status = await session.scalar(text(
            "SELECT status FROM traces WHERE trace_id='deadbeef'"))
        assert status == "timeout"
    finally:
        reset_tenant_id(token)
