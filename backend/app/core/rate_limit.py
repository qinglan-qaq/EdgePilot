"""Redis 固定窗口限流。

限流是保护性旁路：Redis 不可用时失败开放（fail-open），不阻断业务。
"""

import logging

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.exceptions import RateLimited

logger = logging.getLogger(__name__)


def _client() -> aioredis.Redis:
    return aioredis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


async def limit(key: str, *, times: int = 10, seconds: int = 60) -> None:
    """固定窗口计数：窗口内超过 ``times`` 次抛 ``RateLimited``。"""
    client: aioredis.Redis | None = None
    try:
        client = _client()
        redis_key = f"rl:{key}"
        count = await client.incr(redis_key)
        if count == 1:
            await client.expire(redis_key, seconds)
        if count > times:
            raise RateLimited("请求过于频繁")
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - 限流不可用时不阻断业务
        logger.warning("rate limit unavailable, failing open: %s", exc)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败无需影响调用方
                pass
