"""孤儿记录补偿：把超时仍处于 running 的 trace/span 标记为 timeout。

后台全局扫描，不做租户过滤；事务由调用方控制（本函数只执行、不提交）。
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ORPHAN_ERROR = '{"code": "orphaned", "message": "orphaned span"}'


async def sweep_orphans(
    session: AsyncSession,
    now: datetime | None = None,
    timeout_minutes: int = 10,
) -> int:
    """将 start_time 早于 cutoff 且仍 running 的 trace/span 标记为 timeout。

    返回两张表受影响行数之和。
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=timeout_minutes)
    params = {"now": now, "cutoff": cutoff}

    traces = await session.execute(
        text(
            "UPDATE traces SET status = 'timeout', end_time = :now,"
            " updated_at = :now, error_count = error_count + 1"
            " WHERE status = 'running' AND start_time < :cutoff"
            " RETURNING id"
        ),
        params,
    )
    spans = await session.execute(
        text(
            "UPDATE spans SET status = 'timeout', end_time = :now,"
            " updated_at = :now,"
            f" error = '{_ORPHAN_ERROR}'::jsonb"
            " WHERE status = 'running' AND start_time < :cutoff"
            " RETURNING id"
        ),
        params,
    )
    return len(traces.fetchall()) + len(spans.fetchall())
