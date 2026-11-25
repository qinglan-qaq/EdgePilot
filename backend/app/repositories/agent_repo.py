"""``agent_runs`` 行写入。

单次 Agent 试跑/编排执行都复用这里，保证落库字段口径一致：
``input`` 为原始任务、``output`` 为通过校验的结构化结果或失败时的部分原文、
``span_id`` / ``trace_id`` 的空串统一转 ``None``（列宽分别只有 16 / 32）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from app.models.run import AgentRun
from app.repositories.base import BaseRepository


class AgentRunRepo(BaseRepository[AgentRun]):
    model = AgentRun

    async def create_run(
        self,
        *,
        tenant_id: uuid.UUID,
        agent_id: int,
        status: str,
        agent_version: int | None = None,
        input: dict | None = None,
        output: dict | None = None,
        error: str | None = None,
        span_id: str | None = None,
        trace_id: str | None = None,
        total_tokens: int | None = None,
        total_cost: Decimal | None = None,
        retry_count: int = 0,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> AgentRun:
        """插入一行并 flush 取得自增 id；提交时机由调用方决定。"""
        row = AgentRun(
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_version=agent_version,
            status=status,
            input=input,
            output=output,
            error=error,
            span_id=span_id or None,
            trace_id=trace_id or None,
            total_tokens=total_tokens,
            total_cost=total_cost,
            retry_count=retry_count,
            started_at=started_at,
            finished_at=finished_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row
