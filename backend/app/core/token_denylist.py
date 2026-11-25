"""访问令牌 denylist（Redis）。

Redis 不可用时一律降级：写失败只记日志，读失败返回 False，
绝不因基础设施故障阻断认证主流程。
"""

import logging

import redis.asyncio as aioredis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "deny:"


def _client() -> aioredis.Redis:
    return aioredis.from_url(
        get_settings().redis_url,
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )


async def _aclose(client: aioredis.Redis) -> None:
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - 关闭失败无需影响调用方
        pass


async def deny(jti: str, ttl_seconds: int) -> None:
    """把 jti 写入 denylist，TTL 至少 1 秒。"""
    client = _client()
    try:
        await client.setex(f"{_KEY_PREFIX}{jti}", max(1, int(ttl_seconds)), "1")
    except Exception as exc:  # noqa: BLE001 - denylist 故障不阻断业务
        logger.warning("token denylist write failed: %s", exc)
    finally:
        await _aclose(client)


async def is_denied(jti: str | None) -> bool:
    """jti 在 denylist 中返回 True；Redis 不可用返回 False。"""
    if not jti:
        return False
    client = _client()
    try:
        return bool(await client.exists(f"{_KEY_PREFIX}{jti}"))
    except Exception as exc:  # noqa: BLE001 - denylist 故障不阻断业务
        logger.warning("token denylist read failed: %s", exc)
        return False
    finally:
        await _aclose(client)
