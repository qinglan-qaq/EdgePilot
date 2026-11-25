"""观测层的进程级入口。业务代码只从这里拿 tracer。"""
from app.observability.collector import EventCollector
from app.observability.tracer import Tracer

_tracer: Tracer | None = None


class NullWriter:
    """观测 P2 之前的占位写入器：记录被丢弃，不影响业务。"""

    async def write(self, records) -> None:
        return None


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        from app.core.config import get_settings

        s = get_settings()
        _tracer = Tracer(
            enabled=s.obs_enabled,
            sample_rate=s.obs_sample_rate,
            capture_mode=s.obs_capture_mode,
            collector=EventCollector(
                maxsize=s.obs_queue_size,
                batch_size=s.obs_batch_size,
                flush_ms=s.obs_flush_ms,
                writer=NullWriter(),
            ),
        )
    return _tracer


def set_writer(writer) -> None:
    """观测 P2 用真实 BatchWriter 替换 NullWriter；测试也用它注入 FakeWriter。"""
    get_tracer().collector.set_writer(writer)


def reset_tracer() -> None:
    global _tracer
    _tracer = None
