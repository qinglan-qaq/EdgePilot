"""Supervisor 并行取证契约测试（Task 15）。

用注入的 planner / runner 驱动两个并行 ``Send`` 分支，断言：

* 两个 ``agent.*`` span 同属一条 trace、父节点都是 ``supervisor.run``，
  且时间区间真实重叠（证明确实并行，而不是顺序执行）；
* 单个分支超时只产生 ``branch_timeout`` 失败条目，快分支结果保留，
  整图收敛为 ``partial`` 而不抛异常。

全部用例在进程内完成，不触网、不碰数据库，整体运行 < 1s。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.agents.supervisor import Assignment, Supervisor, SupervisorLimits
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.tracer import Tracer

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakeCollector:
    """最小采集器：``Tracer.emit`` 只需要 ``degraded`` 与 ``put_nowait``。"""

    degraded = False

    def __init__(self) -> None:
        self.records: list[dict] = []

    def put_nowait(self, record: dict) -> bool:
        self.records.append(record)
        return True


def _planner(assignments: list[Assignment]):
    async def plan(task: dict, *, failed=None, replans: int = 0):
        return list(assignments)

    return plan


def _success(assignment: Assignment) -> SimpleNamespace:
    return SimpleNamespace(
        status="succeeded",
        output={"answer": f"{assignment.agent_type}:{assignment.goal}"},
        error=None,
        agent_run_id=1,
        total_tokens=10,
        total_cost=None,
    )


class SlowRunner:
    """两个分支都睡 ~0.1s，用于验证 span 时间区间重叠。"""

    async def __call__(self, assignment: Assignment) -> SimpleNamespace:
        await asyncio.sleep(0.1)
        return _success(assignment)


class MixedRunner:
    """``slow`` 分支挂起 1s，``fast`` 分支立即返回；超时必须只影响慢分支。"""

    async def __call__(self, assignment: Assignment) -> SimpleNamespace:
        if assignment.goal == "slow":
            await asyncio.sleep(1)
        else:
            await asyncio.sleep(0.01)
        return _success(assignment)


def _span_closes(collector: FakeCollector, names: set[str] | None = None) -> list[dict]:
    closes = [r for r in collector.records if r.get("kind") == "span.close"]
    if names is None:
        return closes
    return [r for r in closes if r.get("name") in names]


@pytest.mark.asyncio
async def test_parallel_agent_spans_share_supervisor_parent():
    """两个并行 agent span 的父节点都是 ``supervisor.run``，且时间区间重叠。"""
    collector = FakeCollector()
    tracer = Tracer(enabled=True, sample_rate=1.0, collector=collector)
    assignments = [
        Assignment(agent_type="device", goal="查设备"),
        Assignment(agent_type="vision", goal="查画面"),
    ]
    supervisor = Supervisor(
        planner=_planner(assignments),
        runner=SlowRunner(),
        tracer=tracer,
        llm=None,
    )

    token = set_tenant_id(TENANT)
    try:
        async with tracer.start_trace("test"):
            result = await supervisor.run({"question": "并行取证"})
    finally:
        reset_tenant_id(token)

    assert result.status == "succeeded"
    assert len(result.results) == 2

    supervisor_span = _span_closes(collector, {"supervisor.run"})
    assert len(supervisor_span) == 1
    supervisor_record = supervisor_span[0]

    agent_spans = _span_closes(collector, {"agent.device", "agent.vision"})
    assert len(agent_spans) == 2, "两个并行分支必须各有一条 agent span"

    # 父节点唯一：不是根 span，也不是彼此；必须都挂在 supervisor.run 下。
    parents = {record["parent_span_id"] for record in agent_spans}
    assert parents == {supervisor_record["span_id"]}
    assert {record["name"] for record in agent_spans} == {"agent.device", "agent.vision"}

    span_ids = {record["span_id"] for record in agent_spans}
    assert len(span_ids) == 2
    assert all(record["trace_id"] == supervisor_record["trace_id"] for record in agent_spans)

    intervals = [
        (record["start_time"], record["start_time"] + timedelta(milliseconds=record["duration_ms"]))
        for record in agent_spans
    ]
    overlap_start = max(start for start, _ in intervals)
    overlap_end = min(end for _, end in intervals)
    assert overlap_start < overlap_end, "两个分支的 span 时间区间必须真实重叠"


@pytest.mark.asyncio
async def test_branch_timeout_does_not_fail_whole_graph():
    """慢分支 ``branch_timeout`` 只产生失败条目；快分支结果保留、整图不抛异常。"""
    collector = FakeCollector()
    tracer = Tracer(enabled=True, sample_rate=1.0, collector=collector)
    assignments = [
        Assignment(agent_type="device", goal="slow"),
        Assignment(agent_type="vision", goal="fast"),
    ]
    supervisor = Supervisor(
        planner=_planner(assignments),
        runner=MixedRunner(),
        tracer=tracer,
        llm=None,
        limits=SupervisorLimits(branch_timeout_s=0.05),
    )

    token = set_tenant_id(TENANT)
    started = time.perf_counter()
    try:
        async with tracer.start_trace("test"):
            result = await supervisor.run({"question": "超时兜底"})
    finally:
        reset_tenant_id(token)
    elapsed = time.perf_counter() - started

    # 慢分支挂起 1s，但整图必须在分支超时后立即收敛，不能被拖到 1s。
    assert elapsed < 0.8

    # 快分支结果必须保留；慢分支以 branch_timeout 失败而不是整图崩溃。
    fast_results = [
        entry for entry in result.results
        if entry.get("goal") == "fast" and entry.get("status") == "succeeded"
    ]
    assert fast_results
    assert any("branch_timeout" in error for error in result.errors)
    assert result.status in {"partial", "failed"}
    assert result.status != "succeeded"

    timeout_spans = [
        record for record in _span_closes(collector, {"agent.device"})
        if isinstance(record.get("error"), dict)
        and record["error"].get("code") == "branch_timeout"
    ]
    assert timeout_spans, "超时分支的 span 必须标注 branch_timeout"
    assert timeout_spans[0]["error"]["retryable"] is True
