import asyncio
import logging

from app.observability.metrics import (
    event_dropped,
    observability_collector_degraded,
    observability_degraded_transitions_total,
)

logger = logging.getLogger(__name__)

# 连续写失败达到该批次数后进入降级模式（采集模式降到 METADATA）。
DEGRADE_THRESHOLD = 5


class EventCollector:
    def __init__(self, *, maxsize: int, batch_size: int, flush_ms: int, writer,
                 retry_delays: tuple[float, ...] = (0.2, 1.0)) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self._batch_size = batch_size
        self._flush_s = flush_ms / 1000
        self._writer = writer
        self._retry_delays = retry_delays
        self._task: asyncio.Task | None = None
        self.dropped = {"queue_full": 0, "write_failed": 0}
        self.degraded = False
        self._consecutive_failures = 0

    def set_writer(self, writer) -> None:
        self._writer = writer

    def qsize(self) -> int:
        return self._queue.qsize()

    def pending(self) -> list[dict]:
        """取出尚未写入的记录（测试与优雅停机用；生产写路径不走这里）。"""
        items: list[dict] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    def put_nowait(self, record: dict) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self.dropped["queue_full"] += 1
            return False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # 停机收尾：把队列里剩下的记录写完，避免最后一批丢失
        while True:
            rest = self.pending()
            if not rest:
                return
            await self._write_with_retry(rest)

    async def _run(self) -> None:
        while True:
            batch: list[dict] = []
            deadline = asyncio.get_running_loop().time() + self._flush_s
            while len(batch) < self._batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                except asyncio.TimeoutError:
                    break
            if batch:
                await self._write_with_retry(batch)

    async def _write_with_retry(self, batch: list[dict]) -> None:
        for attempt in range(3):
            try:
                await self._writer.write(batch)
                self._consecutive_failures = 0
                self._recover_if_degraded()  # 写入恢复后自动退出降级
                return
            except Exception:
                if attempt == 2:
                    self.dropped["write_failed"] += len(batch)
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= DEGRADE_THRESHOLD:
                        self._enter_degraded()
                    event_dropped.labels(reason="write_failed").inc(len(batch))
                    return
                await asyncio.sleep(self._retry_delays[attempt])

    def _enter_degraded(self) -> None:
        """边沿触发：仅在首次达到阈值时记录一次降级事件/指标。"""
        if self.degraded:
            return
        self.degraded = True
        observability_collector_degraded.set(1)
        observability_degraded_transitions_total.labels(state="degraded").inc()
        logger.warning(
            "观测采集进入降级模式：连续 %d 批写入失败，采集模式降为 METADATA",
            self._consecutive_failures,
        )

    def _recover_if_degraded(self) -> None:
        """边沿触发：仅在从降级恢复时记录一次恢复事件/指标。"""
        if not self.degraded:
            return
        self.degraded = False
        observability_collector_degraded.set(0)
        observability_degraded_transitions_total.labels(state="recovered").inc()
        logger.info("观测采集已恢复：写入成功，返回配置的采集模式")
