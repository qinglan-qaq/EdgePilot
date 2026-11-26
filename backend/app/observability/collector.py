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
    """观测记录的内存缓冲队列：业务线程只入队，后台任务批量落库。

    关键节点：
        1. ``put_nowait`` 永不阻塞业务调用方，队列满时只累加丢弃计数；
        2. 后台 ``_run`` 按 batch_size / flush_ms 攒批，兼顾时延与吞吐；
        3. 写入失败重试 3 次；连续多批失败后进入降级（采集模式降为 METADATA）；
        4. 降级是带边沿触发的状态，恢复成功后自动退出并记录指标。
    """

    def __init__(self, *, maxsize: int, batch_size: int, flush_ms: int, writer,
                 retry_delays: tuple[float, ...] = (0.2, 1.0)) -> None:
        """初始化采集队列。

        Args:
            maxsize: 队列容量上限，满后新记录直接丢弃。
            batch_size: 单批最大记录数。
            flush_ms: 攒批最长等待时间，单位毫秒。
            writer: 具备 ``async write(batch)`` 的落库组件。
            retry_delays: 每次重试前的等待秒数，长度决定重试节奏。
        """
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
        """把一条观测记录放入队列；队列满时丢弃而不是阻塞业务线程。

        关键节点：
            1. 使用非阻塞 put，保证 LLM/工具调用链路不被观测拖慢；
            2. 丢弃只累加 ``queue_full`` 计数，由 ``/metrics`` 暴露。

        Args:
            record: 已脱敏的观测记录（trace.start / span.open 等）。

        Returns:
            True 表示入队成功；False 表示队列已满被丢弃。
        """
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            self.dropped["queue_full"] += 1
            return False

    async def start(self) -> None:
        """启动后台消费任务；重复调用是幂等的。"""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """停止后台任务，并把队列残留记录尽力写完。

        关键节点：
            1. 先取消消费协程，避免停机期间继续抢新记录；
            2. 再循环排空队列并写库，尽量不丢最后一批。
        """
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
        """后台消费循环：攒批后交给 writer 落库，永不退出。

        关键节点：
            1. 每批都有 flush_ms 截止时间，低流量时也不会无限等待；
            2. 达到 batch_size 立即发车，高流量时保持吞吐；
            3. 写失败的整批由 ``_write_with_retry`` 负责重试与丢弃计数。
        """
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
        """写入一批记录，失败按固定节奏重试，最终仍失败才丢弃。

        关键节点：
            1. 成功后清零连续失败计数，并尝试退出降级；
            2. 最后一次失败整批丢弃，同时累加降级计数；
            3. 连续失败达到阈值进入降级——此后采集模式压到 METADATA，
               只保留元数据，避免继续放大故障。

        Args:
            batch: 本批待写入的观测记录。
        """
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
