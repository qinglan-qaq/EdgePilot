"""SSE 事件格式化与 trace 事件流。

事件 id 使用 ``sequence_number``，支持 ``Last-Event-ID`` 从 PG 补发；
Redis 实时通道 ``obs:{trace_id}`` 仅做转发，断线时以心跳保活。
"""

import asyncio
import json
from collections.abc import AsyncIterator

from sqlalchemy import select

from app.core.tenancy import get_tenant_id
from app.observability.models import TraceEvent

DONE_EVENT = "event: done\ndata: {}\n\n"
PING_EVENT = ": ping\n\n"
HEARTBEAT_SECONDS = 15

_EVENT_FIELDS = ("trace_id", "span_id", "event_type", "event_data", "sequence_number")


def format_event(sequence_number: int, payload: dict) -> str:
    """单条 SSE 事件；payload 序列化为单行合法 JSON。"""
    return f"id: {sequence_number}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _coerce_last_event_id(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _event_payload(row: TraceEvent) -> dict:
    return {field: getattr(row, field) for field in _EVENT_FIELDS}


async def _fetch_events(session_factory, tenant_id, trace_id: str, after: int):
    async with session_factory() as session:
        return (
            await session.scalars(
                select(TraceEvent)
                .where(
                    TraceEvent.tenant_id == tenant_id,
                    TraceEvent.trace_id == trace_id,
                    TraceEvent.sequence_number > after,
                )
                .order_by(TraceEvent.sequence_number.asc())
            )
        ).all()


async def stream_trace_events(
    session_factory,
    pubsub,
    trace_id: str,
    last_event_id: str | int | None = None,
) -> AsyncIterator[str]:
    """先补发 PG 中的历史事件，再转发 Redis 实时消息。

    关键节点：
        1. 用 Last-Event-ID 从 PG 补发断点之后的历史事件，支持刷新续传；
        2. 订阅本 trace 的 Redis 频道后转为 SSE，实时事件与历史事件同格式；
        3. 15 秒无消息产出心跳，避免代理层判定连接空闲；
        4. 收到 ``trace.close`` 后发 done 并结束，前端可据此停止重连。

    Args:
        session_factory: 异步 session 工厂，用于读取历史事件。
        pubsub: Redis pubsub 连接，用于订阅实时频道。
        trace_id: 要订阅的 Trace ID。
        last_event_id: 客户端已收到的最大序号；为空表示从头补发。

    Yields:
        已格式化好的 SSE 文本行（data / ping / done）。
    """
    tenant_id = get_tenant_id()
    channel = f"obs:{trace_id}"
    try:
        for row in await _fetch_events(
            session_factory, tenant_id, trace_id, _coerce_last_event_id(last_event_id)
        ):
            yield format_event(row.sequence_number, _event_payload(row))

        await pubsub.subscribe(channel)
        listener = pubsub.listen()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        listener.__anext__(), timeout=HEARTBEAT_SECONDS
                    )
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    yield PING_EVENT
                    continue

                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message.get("data") or "{}")
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("kind") == "trace.close":
                    yield DONE_EVENT
                    return
                yield format_event(
                    _coerce_last_event_id(payload.get("sequence_number")), payload
                )
        finally:
            try:
                await listener.aclose()
            except Exception:
                pass
    finally:
        try:
            await pubsub.unsubscribe(channel)
        except Exception:
            pass
