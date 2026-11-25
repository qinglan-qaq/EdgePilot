"""心跳守卫单元测试：用假会话验证周期续租，不依赖真实数据库连接。

真实 keeper 需要在独立连接上写入已提交的节点行；测试事务尚未提交，
因此这里只验证「周期性 execute + commit，且更新目标为指定 node id」。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from app.workflow.engine import WorkflowEngine


class _FakeSession:
    def __init__(self) -> None:
        self.executions: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Any) -> None:
        self.executions.append(statement)
        return None

    async def commit(self) -> None:
        self.commits += 1


class _FakeFactory:
    """模拟 async_sessionmaker：每次调用返回一个异步上下文管理器。"""

    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.entries = 0

    def __call__(self):
        @asynccontextmanager
        async def _manager():
            self.entries += 1
            yield self.session

        return _manager()


async def test_heartbeat_loop_refreshes_target_node():
    fake_session = _FakeSession()
    factory = _FakeFactory(fake_session)
    engine = WorkflowEngine(
        session=object(),  # 循环只用心跳工厂，不触碰引擎主会话
        heartbeat_factory=factory,
        heartbeat_interval_s=0.01,
        stale_after_s=60,
    )

    task = engine._start_heartbeat(42)
    assert task is not None
    await asyncio.sleep(0.05)
    await engine._stop_heartbeat(task)

    assert fake_session.commits >= 1
    assert fake_session.executions
    compiled = fake_session.executions[0].compile()
    assert "workflow_node_runs" in str(compiled)
    assert 42 in compiled.params.values()
