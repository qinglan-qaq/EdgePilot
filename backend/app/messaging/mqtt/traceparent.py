"""W3C ``traceparent`` 生成与透传（下行指令用）。

优先级由调用方决定：请求体 → ``traceparent`` 请求头 → 当前活动 trace →
新生成。租户信息永远来自 JWT / 显式参数，不采信 header。
"""

from __future__ import annotations

import re
import secrets

from app.observability import context as obs_context

_PATTERN = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16


def is_valid_traceparent(value: str | None) -> bool:
    if not isinstance(value, str) or not _PATTERN.match(value):
        return False
    trace_id, span_id = value[3:35], value[36:52]
    return trace_id != _ZERO_TRACE and span_id != _ZERO_SPAN


def generate_traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def current_traceparent() -> str | None:
    """读取观测层 ContextVar；无活动 trace 时返回 None。"""
    trace = obs_context.current_trace.get()
    trace_id = getattr(trace, "trace_id", None)
    if not trace_id:
        return None
    span = obs_context.current_span.get()
    span_id = getattr(span, "span_id", None)
    if not span_id or getattr(span, "trace_id", None) != trace_id:
        span_id = getattr(trace, "root_span_id", None)
    if not span_id:
        return None
    return f"00-{trace_id}-{span_id}-01"


def resolve_traceparent(*candidates: str | None) -> str:
    """按给定顺序取第一个合法值，最后回退到当前 trace 或新生成。"""
    for candidate in candidates:
        if is_valid_traceparent(candidate):
            return candidate  # type: ignore[return-value]
    active = current_traceparent()
    if active is not None:
        return active
    return generate_traceparent()
