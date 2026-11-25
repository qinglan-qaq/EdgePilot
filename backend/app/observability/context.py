from contextvars import ContextVar

from app.observability.span import SpanHandle, TraceHandle

current_trace: ContextVar[TraceHandle | None] = ContextVar("obs_current_trace", default=None)
current_span: ContextVar[SpanHandle | None] = ContextVar("obs_current_span", default=None)
sequence: ContextVar[int] = ContextVar("obs_sequence", default=0)


def next_sequence() -> int:
    value = sequence.get() + 1
    sequence.set(value)
    return value
