import asyncio
import pytest

from app.observability.collector import EventCollector


class FakeWriter:
    def __init__(self):
        self.batches = []

    async def write(self, records):
        self.batches.append(list(records))


def test_queue_full_drops_without_raising():
    c = EventCollector(maxsize=1, batch_size=10, flush_ms=1000, writer=FakeWriter())
    assert c.put_nowait({"i": 1}) is True
    assert c.put_nowait({"i": 2}) is False
    assert c.dropped["queue_full"] == 1


@pytest.mark.asyncio
async def test_flushes_by_batch_size():
    writer = FakeWriter()
    c = EventCollector(maxsize=100, batch_size=3, flush_ms=5000, writer=writer)
    await c.start()
    for i in range(3):
        c.put_nowait({"i": i})
    await asyncio.sleep(0.05)
    await c.stop()
    assert [len(b) for b in writer.batches] == [3]


@pytest.mark.asyncio
async def test_write_failure_is_counted_not_raised():
    class Broken:
        async def write(self, records):
            raise RuntimeError("pg down")

    c = EventCollector(maxsize=10, batch_size=1, flush_ms=50, writer=Broken())
    await c.start()
    c.put_nowait({"i": 0})
    await asyncio.sleep(1.5)
    await c.stop()
    assert c.dropped["write_failed"] == 1


@pytest.mark.asyncio
async def test_five_failed_batches_degrade_then_recover():
    class Flaky:
        def __init__(self):
            self.fail = True

        async def write(self, records):
            if self.fail:
                raise RuntimeError("pg down")

    writer = Flaky()
    c = EventCollector(maxsize=10, batch_size=1, flush_ms=10, writer=writer,
                       retry_delays=(0, 0))
    for _ in range(5):
        await c._write_with_retry([{"i": 0}])
    assert c.degraded is True
    assert c.dropped["write_failed"] == 5

    writer.fail = False
    await c._write_with_retry([{"i": 1}])
    assert c.degraded is False
