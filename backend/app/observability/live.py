"""Redis 实时覆盖层：最新快照 + 发布/订阅。

观测链路不允许影响业务：publish/snapshot 的 Redis 异常一律吞掉，
连接超时压到 0.5s。每次操作自建连接并关闭，优先简单可靠。
"""

import json
from collections.abc import AsyncIterator

import redis.asyncio as aioredis

from app.core.config import get_settings

SNAPSHOT_PREFIX = "obs:live:"
CHANNEL_PREFIX = "obs:"


def _client(url: str):
    return aioredis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )


class LiveLayer:
    def __init__(self, redis_url: str, ttl_seconds: int = 3600):
        self.redis_url = redis_url
        self.ttl_seconds = ttl_seconds

    async def publish(self, trace_id: str, payload: dict) -> bool:
        """写快照并广播；失败返回 False，不抛异常。"""
        data = json.dumps(payload, ensure_ascii=False)
        client = _client(self.redis_url)
        try:
            await client.set(
                f"{SNAPSHOT_PREFIX}{trace_id}", data, ex=self.ttl_seconds
            )
            await client.publish(f"{CHANNEL_PREFIX}{trace_id}", data)
            return True
        except Exception:
            return False
        finally:
            await self._close(client)

    async def snapshot(self, trace_id: str) -> dict:
        """读取最新快照；无值或失败返回空 dict。"""
        client = _client(self.redis_url)
        try:
            raw = await client.get(f"{SNAPSHOT_PREFIX}{trace_id}")
        except Exception:
            return {}
        finally:
            await self._close(client)
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    async def subscribe(self, trace_id: str) -> AsyncIterator[dict]:
        """订阅实时通道，逐条产出解析后的 dict；连接失败则直接结束。"""
        channel = f"{CHANNEL_PREFIX}{trace_id}"
        client = _client(self.redis_url)
        pubsub = client.pubsub()
        try:
            try:
                await pubsub.subscribe(channel)
            except Exception:
                return
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    payload = json.loads(message.get("data") or "{}")
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    yield payload
        finally:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                pass
            try:
                await pubsub.aclose()
            except Exception:
                pass
            await self._close(client)

    @staticmethod
    async def _close(client) -> None:
        try:
            await client.aclose()
        except Exception:
            pass


async def get_pubsub():
    """FastAPI 依赖：为 SSE 端点提供独立 pubsub，请求结束关闭。"""
    client = _client(get_settings().redis_url)
    pubsub = client.pubsub()
    try:
        yield pubsub
    finally:
        try:
            await pubsub.aclose()
        except Exception:
            pass
        try:
            await client.aclose()
        except Exception:
            pass
