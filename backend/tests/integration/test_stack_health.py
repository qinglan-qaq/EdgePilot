"""依赖栈健康检查：验证测试库与 Redis 可真实连通。"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_database_reachable(session):
    assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_redis_reachable(redis_client):
    assert await redis_client.ping() is True
