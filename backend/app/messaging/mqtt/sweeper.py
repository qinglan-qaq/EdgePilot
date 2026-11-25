"""节点离线扫描：15s 未收到心跳置 offline（LWT 是第二重保障）。

这是跨租户系统任务，不读租户 ContextVar；单次 ``sweep_once`` 可独立测试。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.device import EdgeNode

logger = logging.getLogger(__name__)


async def sweep_once(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    offline_after_s: float | None = None,
) -> int:
    """将超时未心跳的 online 节点置 offline；disabled 节点不动。"""
    settings = get_settings()
    now = now or datetime.now(timezone.utc)
    timeout = float(offline_after_s if offline_after_s is not None else settings.node_offline_after_s)
    cutoff = now - timedelta(seconds=timeout)

    stmt = (
        update(EdgeNode)
        .where(
            EdgeNode.status == "online",
            EdgeNode.last_heartbeat_at.is_not(None),
            EdgeNode.last_heartbeat_at < cutoff,
            func.coalesce(EdgeNode.labels["disabled"].astext, "false") != "true",
        )
        .values(status="offline", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount or 0)


class NodeSweeper:
    """后台任务：每 ``interval_s`` 执行一次 ``sweep_once``。"""

    def __init__(
        self,
        session_factory,
        *,
        offline_after_s: float | None = None,
        interval_s: float | None = None,
    ) -> None:
        settings = get_settings()
        self.session_factory = session_factory
        self.offline_after_s = float(
            offline_after_s if offline_after_s is not None else settings.node_offline_after_s
        )
        self.interval_s = float(
            interval_s if interval_s is not None else settings.node_sweep_interval_s
        )
        if self.interval_s <= 0:
            raise ValueError("interval_s 必须为正数")
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="node-sweeper")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 停机阶段不向上抛
            logger.exception("node sweeper 停止异常")

    async def _run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.interval_s)
            if self._stopping:
                break
            await self.tick()

    async def tick(self) -> int:
        try:
            async with self.session_factory() as session:
                count = await sweep_once(session, offline_after_s=self.offline_after_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单次扫描失败不终止后台任务
            logger.exception("node sweeper 扫描失败")
            return 0
        if count:
            logger.info("node sweeper 将 %d 个节点置为 offline", count)
        return count
