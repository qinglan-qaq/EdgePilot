"""Supervisor 预算硬上限契约测试（Task 14）。

全部使用注入的 planner / runner，不触网、不碰数据库；
第 7 次派发必须在派发**前**被代码拒绝，而不是靠 prompt 自觉。
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.agents.supervisor import Assignment, Supervisor, SupervisorLimits


@dataclasses.dataclass
class FakeRun:
    """``AgentRunResult`` 的最小替身；只保留 Supervisor 聚合用到的字段。"""

    status: str = "succeeded"
    output: dict | None = None
    error: str | None = None
    agent_run_id: int | None = 1
    total_tokens: int | None = 100
    total_cost: Any = None
    trace_id: str | None = None
    span_id: str | None = None


class FakeRunner:
    """记录调用次数；按固定规则返回成功/失败结果。"""

    def __init__(self, *, status: str = "succeeded", total_tokens: int | None = 100):
        self.calls: list[Assignment] = []
        self._status = status
        self._total_tokens = total_tokens

    async def __call__(self, assignment: Assignment) -> FakeRun:
        self.calls.append(assignment)
        if self._status == "succeeded":
            return FakeRun(
                status="succeeded",
                output={"goal": assignment.goal, "value": len(self.calls)},
                total_tokens=self._total_tokens,
            )
        return FakeRun(
            status="failed",
            output=None,
            error=f"runner_failed: {assignment.goal}",
            total_tokens=self._total_tokens,
        )


def _planner(assignments: list[Assignment], calls: list[dict] | None = None):
    """构造一个固定返回同一批 Assignment 的 planner，并记录调用参数。"""

    async def plan(task: dict, *, failed=None, replans: int = 0):
        if calls is not None:
            calls.append({"task": task, "failed": failed, "replans": replans})
        return list(assignments)

    return plan


def _assignments(count: int, agent_type: str = "device") -> list[Assignment]:
    return [
        Assignment(agent_type=agent_type, goal=f"子任务 {index}")
        for index in range(1, count + 1)
    ]


@pytest.mark.asyncio
async def test_seventh_agent_call_is_rejected_with_partial_results():
    """7 次派发只执行前 6 次，第 7 次在入队前被拒；结果保留 6 条部分结果。"""
    runner = FakeRunner()
    supervisor = Supervisor(
        planner=_planner(_assignments(7)),
        runner=runner,
        llm=None,
        limits=SupervisorLimits(),
    )

    result = await supervisor.run({"question": "检查设备"})

    assert len(runner.calls) == 6
    assert result.agent_calls == 6
    assert len(result.results) == 6
    assert result.status == "partial"
    assert any("agent_call_limit" in error for error in result.errors)
    assert result.total_tokens == 6 * 100


@pytest.mark.asyncio
async def test_replans_stop_at_two():
    """失败重规划最多 2 次：planner 总共最多被调用 3 次（初始 + 2 次重规划）。"""
    planner_calls: list[dict] = []
    runner = FakeRunner(status="failed", total_tokens=10)
    supervisor = Supervisor(
        planner=_planner([Assignment(agent_type="diagnosis", goal="定位根因")], planner_calls),
        runner=runner,
        llm=None,
        limits=SupervisorLimits(),
    )

    result = await supervisor.run({"question": "为什么离线"})

    assert len(planner_calls) == 3
    assert result.replans == 2
    assert len(runner.calls) == 3
    assert result.status != "succeeded"
    assert all(entry["status"] == "failed" for entry in result.results)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["tool", "supervisor"])
async def test_invalid_assignment_is_rejected(agent_type: str):
    """Supervisor 没有数据工具权限，也不允许自调用；必须在派发前拒绝。"""
    runner = FakeRunner()
    supervisor = Supervisor(
        planner=_planner([Assignment(agent_type=agent_type, goal="越权调用")]),
        runner=runner,
        llm=None,
        limits=SupervisorLimits(),
    )

    result = await supervisor.run({"question": "越权"})

    assert runner.calls == []
    assert result.agent_calls == 0
    assert result.status == "failed"
    assert any("supervisor_invalid_assignment" in error for error in result.errors)


@pytest.mark.asyncio
async def test_token_budget_stops_further_replanning():
    """单次 35k token：第二波累计 70k 超预算，禁止第 2 次重规划。"""
    planner_calls: list[dict] = []
    runner = FakeRunner(status="failed", total_tokens=35000)
    supervisor = Supervisor(
        planner=_planner([Assignment(agent_type="operations", goal="查节点指标")], planner_calls),
        runner=runner,
        llm=None,
        limits=SupervisorLimits(),
    )

    result = await supervisor.run({"question": "节点为什么慢"})

    # 初始 1 次 + 1 次重规划；第二波后累计 70000 > 60000，不再重规划
    assert len(planner_calls) == 2
    assert result.replans == 1
    assert len(result.results) == 2
    assert result.total_tokens == 70000
    assert any("budget_exceeded" in error for error in result.errors)
    assert result.status in {"partial", "failed"}
