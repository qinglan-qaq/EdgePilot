"""MQTT 消息去重。

主实现是 Redis ``SET NX EX``（默认 24h）；Redis 不可用时降级为进程内有界
LRU，保证业务不被去重基础设施阻塞。测试可直接注入 :class:`MemoryDedupe`。
本项目不新增去重表。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 86400
DEFAULT_MAX_ENTRIES = 10000


def build_dedupe_key(tenant_id: uuid.UUID | str, node_key: str, msg_id: str) -> str:
    return f"mqtt:dedupe:{tenant_id}:{node_key}:{msg_id}"


class MemoryDedupe:
    """进程内有界去重；``try_claim`` 返回 True 表示首次出现。"""

    def __init__(
        self,
        maxsize: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize 必须为正数")
        self.maxsize = int(maxsize)
        self.ttl_seconds = int(ttl_seconds)
        self._clock = clock or time.monotonic
        self._entries: OrderedDict[str, float] = OrderedDict()

    async def try_claim(self, key: str, *, ttl_seconds: int | None = None) -> bool:
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        now = self._clock()
        self._purge(now)

        expiry = self._entries.get(key)
        if expiry is not None and expiry > now:
            return False
        if expiry is not None:
            self._entries.pop(key, None)

        self._entries[key] = now + ttl
        self._entries.move_to_end(key)
        while len(self._entries) > self.maxsize:
            self._entries.popitem(last=False)
        return True

    async def close(self) -> None:
        self._entries.clear()

    def _purge(self, now: float) -> None:
        expired = [key for key, expiry in self._entries.items() if expiry <= now]
        for key in expired:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)


class RedisDedupe:
    """Redis 去重 + 失败降级。

    ``_retry_after`` 是简单熔断：Redis 超时后短暂跳过 Redis，避免每条消息都
    等连接超时；熔断期间直接使用内存 fallback。
    """

    def __init__(
        self,
        redis_url: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        fallback: MemoryDedupe | None = None,
        client_factory: Callable[[str], aioredis.Redis] | None = None,
        retry_cooldown_s: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.ttl_seconds = int(ttl_seconds)
        self.fallback = fallback or MemoryDedupe(ttl_seconds=ttl_seconds)
        self._client_factory = client_factory or self._default_client
        self._client: aioredis.Redis | None = None
        self._retry_cooldown_s = float(retry_cooldown_s)
        self._retry_after = 0.0
        self._clock = clock or time.monotonic

    @staticmethod
    def _default_client(url: str) -> aioredis.Redis:
        return aioredis.from_url(
            url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = self._client_factory(self.redis_url)
        return self._client

    async def try_claim(self, key: str, *, ttl_seconds: int | None = None) -> bool:
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        if self._clock() < self._retry_after:
            return await self.fallback.try_claim(key, ttl_seconds=ttl)

        try:
            client = self._get_client()
            result = await client.set(key, "1", nx=True, ex=ttl)
            return bool(result)
        except Exception as exc:  # noqa: BLE001 - 去重不可用时必须继续处理消息
            self._retry_after = self._clock() + self._retry_cooldown_s
            logger.warning("redis 去重不可用，降级为内存去重: %s", exc)
            return await self.fallback.try_claim(key, ttl_seconds=ttl)

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不影响停机
                pass
            self._client = None
        await self.fallback.close()


# 供类型提示与依赖注入使用的轻量协议语义：对象只需实现 try_claim/close。
DedupeStore = RedisDedupe | MemoryDedupe
