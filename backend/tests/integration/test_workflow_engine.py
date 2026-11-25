"""工作流引擎集成测试：图推进、审批暂停/恢复、重试、幂等与观测隔离。

测试只使用真实数据库事务与注入的 Agent/Tool 测试替身；绝不调用真实
LLM、MQTT、Redis 或 Celery broker。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

from app.agents.base import AgentRunResult
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.agent import Agent
from app.models.run import WorkflowNodeRun, WorkflowRun
from app.models.tool import Tool
from app.services.run_service import RunService
from app.tools.executor import ToolExecutionError
from app.tools.registry import ToolResult
from app.workflow.engine import WorkflowEngine
from tests.conftest import TENANT, auth_headers

AGENT_NAME = "engine-agent"
TOOL_ID = "echo_tool"


# ------------------------------------------------------------------ 测试替身


class FakeSpan:
    def __init__(self, name: str, span_type: str, **kwargs: Any) -> None:
        self.name = name
        self.span_type = span_type
        self.span_id = uuid.uuid4().hex[:16]
        self.kwargs = kwargs
        self.statuses: list[str] = []
        self.inputs: list[Any] = []
        self.outputs: list[Any] = []
        self.errors: list[tuple] = []
        self.meta: dict = {}
        self.events: list[tuple] = []

    def set_input(self, value: Any) -> "FakeSpan":
        self.inputs.append(value)
        return self

    def set_output(self, value: Any) -> "FakeSpan":
        self.outputs.append(value)
        return self

    def set_error(self, code: str, message: str, *, retryable: bool = False) -> "FakeSpan":
        self.errors.append((code, message, retryable))
        return self

    def set_meta(self, **kv: Any) -> "FakeSpan":
        self.meta.update(kv)
        return self

    def add_event(self, event_type: str, data: dict) -> "FakeSpan":
        self.events.append((event_type, data))
        return self

    def finish(self, status: str = "succeeded") -> None:
        self.statuses.append(status)


class FakeTrace:
    trace_id = "a" * 32
    root_span_id = "b" * 16

    def __init__(self) -> None:
        self.closed: list[str] = []
        self.output: Any = None

    def set_output(self, value: Any) -> "FakeTrace":
        self.output = value
        return self

    def close(self, status: str = "succeeded") -> None:
        self.closed.append(status)


class FakeTracer:
    def __init__(self) -> None:
        self.trace = FakeTrace()
        self.trace_starts: list[dict] = []
        self.spans: list[FakeSpan] = []
        self.records: list[dict] = []

    def start_trace(self, name, **kwargs):
        self.trace_starts.append({"name": name, **kwargs})
        return self.trace

    def start_span(self, name, span_type, **kwargs):
        span = FakeSpan(name, span_type, **kwargs)
        self.spans.append(span)
        return span

    def emit(self, record: dict) -> bool:
        self.records.append(record)
        return True


class ExplodingSpanTracer(FakeTracer):
    def start_span(self, name, span_type, **kwargs):
        raise RuntimeError("span 写入失败")


def agent_result(output: Any = None, *, status: str = "succeeded", error: str | None = None):
    return AgentRunResult(
        agent_run_id=1,
        status=status,
        output=output,
        error=error,
        trace_id=None,
        span_id=None,
        total_tokens=None,
        total_cost=None,
        retry_count=0,
    )


def agent_runner(outputs: list[Any], *, on_call=None):
    """按顺序返回成功输出；字符串视为失败错误信息。"""
    calls: list[dict] = []

    async def runner(agent, task, **kwargs):
        calls.append({"agent": agent, "task": task, "kwargs": kwargs})
        if on_call is not None:
            await on_call(len(calls))
        item = outputs.pop(0)
        if isinstance(item, str):
            return agent_result(None, status="failed", error=item)
        return agent_result(item)

    return runner, calls


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def tenant_context():
    token = set_tenant_id(TENANT)
    yield TENANT
    reset_tenant_id(token)


async def _seed_agent(session) -> None:
    session.add(
        Agent(
            tenant_id=TENANT,
            name=AGENT_NAME,
            agent_type="device",
            status="active",
        )
    )
    await session.flush()


async def _seed_tool(session) -> None:
    session.add(
        Tool(
            tenant_id=TENANT,
            tool_id=TOOL_ID,
            name="回显工具",
            source="local",
            enabled=True,
        )
    )
    await session.flush()


async def _create_run(
    client,
    token: str,
    session,
    dsl: dict,
    *,
    input_data: dict | None = None,
) -> int:
    created = await client.post(
        "/api/v1/workflows",
        json={"name": f"wf-{uuid.uuid4().hex[:8]}", "dsl": dsl},
        headers=auth_headers(token),
    )
    assert created.status_code == 200, created.text
    workflow_id = created.json()["data"]["id"]

    published = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish",
        headers=auth_headers(token),
    )
    assert published.status_code == 200, published.text

    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"input": {"question": "q"} if input_data is None else input_data},
        headers=auth_headers(token),
    )
    assert run.status_code == 200, run.text
    return int(run.json()["data"]["id"])


async def _node_rows(session, run_id: int) -> dict[str, WorkflowNodeRun]:
    rows = (
        await session.scalars(
            select(WorkflowNodeRun)
            .where(WorkflowNodeRun.run_id == run_id)
            .order_by(WorkflowNodeRun.id)
        )
    ).all()
    return {row.node_id: row for row in rows}


def _approval_dsl(approvers: list[str] | None = None) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "draft",
                "type": "agent",
                "agent": AGENT_NAME,
                "timeout_ms": 30000,
                "input_map": {"question": "$input.question"},
            },
            {
                "id": "gate",
                "type": "condition",
                "timeout_ms": 1000,
                "expression": "$input.approve == true",
            },
            {
                "id": "review",
                "type": "approval",
                "timeout_ms": 60000,
                "title": "请审批",
                "approvers": approvers or ["admin"],
            },
            {
                "id": "report",
                "type": "agent",
                "agent": AGENT_NAME,
                "timeout_ms": 30000,
                "input_map": {"draft": "$draft.output"},
            },
            {"id": "done", "type": "end", "timeout_ms": 1000},
            {"id": "skip", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "draft"},
            {"from": "draft", "to": "gate"},
            {"from": "gate", "to": "review", "when": "true"},
            {"from": "gate", "to": "skip", "when": "false"},
            {"from": "review", "to": "report"},
            {"from": "report", "to": "done"},
        ],
    }


def _simple_dsl(
    *,
    node_type: str = "agent",
    max_retries: int | None = None,
    timeout_ms: int = 30000,
) -> dict:
    node: dict[str, Any] = {
        "id": "work",
        "type": node_type,
        "timeout_ms": timeout_ms,
        "input_map": {"question": "$input.question", "x": 1},
    }
    if node_type == "agent":
        node["agent"] = AGENT_NAME
    else:
        node["tool"] = TOOL_ID
    if max_retries is not None:
        node["max_retries"] = max_retries
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            node,
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "work"},
            {"from": "work", "to": "end"},
        ],
    }


# ---------------------------------------------------------------- 主流程与审批


async def test_happy_path_pauses_at_approval_then_resume_sets_report_output(
    client, admin_token, session, seeded_users, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(
        client,
        admin_token,
        session,
        _approval_dsl(),
        input_data={"question": "温度过高", "approve": True},
    )
    tracer = FakeTracer()
    runner, calls = agent_runner(
        [{"answer": "草稿"}, {"answer": "最终报告", "ok": True}]
    )

    result = await WorkflowEngine(
        session, run_id, tracer=tracer, agent_runner=runner
    ).run()

    assert result.status == "waiting_approval"
    assert result.waiting_node_id == "review"
    run = await session.get(WorkflowRun, run_id)
    assert run.status == "waiting_approval"
    assert run.trace_id == FakeTrace.trace_id
    assert run.root_span_id == FakeTrace.root_span_id

    nodes = await _node_rows(session, run_id)
    assert nodes["draft"].status == "succeeded"
    assert nodes["review"].status == "waiting_approval"
    approval = nodes["review"].approval
    assert approval["title"] == "请审批"
    assert approval["approvers"] == ["admin"]
    assert approval["decision"] is None
    assert approval["expires_at"] > approval["created_at"]
    assert nodes["report"].status == "pending"
    assert len(calls) == 1
    review_span = next(span for span in tracer.spans if span.name == "node.review")
    assert nodes["review"].span_id == review_span.span_id

    # 审批通过：run 回到 running，节点成功，且只把审批结果写回节点。
    approved = await RunService(session).decide(
        run_id, approve=True, comment="同意", user=seeded_users[0]
    )
    assert approved.run.status == "running"
    assert approved.run.finished_at is None
    review = next(node for node in approved.nodes if node.node_id == "review")
    assert review.status == "succeeded"
    assert review.approval["decision"] == "approved"
    assert review.approval["comment"] == "同意"

    resumed = await WorkflowEngine(
        session, run_id, tracer=tracer, agent_runner=runner
    ).run(resume_node_id="review")

    assert resumed.status == "succeeded"
    assert resumed.output == {"answer": "最终报告", "ok": True}
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "succeeded"
    assert run.output == {"answer": "最终报告", "ok": True}
    nodes = await _node_rows(session, run_id)
    assert nodes["review"].status == "succeeded"
    assert nodes["report"].status == "succeeded"
    assert nodes["done"].status == "succeeded"
    assert len(calls) == 2
    # 恢复时复用已落库 trace，绝不第二次 start_trace。
    assert len(tracer.trace_starts) == 1
    closes = [
        record for record in tracer.records if record.get("kind") == "trace.close"
    ]
    assert len(closes) == 1
    assert closes[0]["status"] == "succeeded"
    assert closes[0]["trace_id"] == FakeTrace.trace_id

    # 恢复执行时补关审批 span，终态 trace 不应残留 waiting_approval。
    approval_closes = [
        record
        for record in tracer.records
        if record.get("kind") == "span.close"
        and record.get("span_type") == "approval"
    ]
    assert len(approval_closes) == 1
    assert approval_closes[0]["status"] == "succeeded"
    assert approval_closes[0]["span_id"] == nodes["review"].span_id
    assert approval_closes[0]["output"] == {"decision": "approved"}


async def test_reject_fails_run_and_skips_downstream_nodes(
    client, admin_token, session, seeded_users, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(
        client,
        admin_token,
        session,
        _approval_dsl(),
        input_data={"question": "q", "approve": True},
    )
    runner, calls = agent_runner([{"answer": "草稿"}])
    await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), agent_runner=runner
    ).run()

    rejected = await RunService(session).decide(
        run_id, approve=False, comment="风险太高", user=seeded_users[0]
    )

    assert rejected.run.status == "failed"
    assert rejected.run.error == "approval_rejected"
    assert rejected.run.finished_at is not None
    nodes = {node.node_id: node for node in rejected.nodes}
    assert nodes["review"].status == "failed"
    assert nodes["review"].error == "approval_rejected"
    assert nodes["report"].status == "skipped"
    assert nodes["done"].status == "skipped"
    assert len(calls) == 1


async def test_condition_false_branch_skips_approval_and_report(
    client, admin_token, session, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(
        client,
        admin_token,
        session,
        _approval_dsl(),
        input_data={"question": "q", "approve": False},
    )
    runner, calls = agent_runner([{"answer": "草稿"}])

    result = await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), agent_runner=runner
    ).run()

    assert result.status == "succeeded", result.error
    assert result.output == {"result": False}
    nodes = await _node_rows(session, run_id)
    assert nodes["gate"].status == "succeeded"
    assert nodes["review"].status == "skipped"
    assert nodes["report"].status == "skipped"
    assert nodes["skip"].status == "succeeded"
    assert len(calls) == 1


# ---------------------------------------------------------------- 重试与幂等


async def test_agent_retries_fail_twice_then_succeed_with_backoff(
    client, admin_token, session, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(
        client, admin_token, session, _simple_dsl(max_retries=2)
    )
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    runner, calls = agent_runner(
        ["provider_error: 第一次失败", "provider_error: 第二次失败", {"answer": "成功"}]
    )

    result = await WorkflowEngine(
        session,
        run_id,
        tracer=FakeTracer(),
        agent_runner=runner,
        sleep=fake_sleep,
    ).run()

    assert result.status == "succeeded"
    assert result.output == {"answer": "成功"}
    assert delays == [1, 4]
    assert len(calls) == 3
    nodes = await _node_rows(session, run_id)
    assert nodes["work"].status == "succeeded"
    assert nodes["work"].attempt == 3

    # 终态幂等：第二次调用不重跑任何节点。
    again = await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), agent_runner=runner
    ).run()
    assert again.status == "succeeded"
    assert len(calls) == 3


async def test_second_engine_call_does_not_reexecute_succeeded_run(
    client, admin_token, session, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(client, admin_token, session, _simple_dsl())
    runner, calls = agent_runner([{"answer": "一次"}])
    tracer = FakeTracer()

    first = await WorkflowEngine(
        session, run_id, tracer=tracer, agent_runner=runner
    ).run()
    second = await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), agent_runner=runner
    ).run()

    assert first.status == second.status == "succeeded"
    assert len(calls) == 1
    nodes = await _node_rows(session, run_id)
    assert nodes["work"].attempt == 1


# ---------------------------------------------------------------- 取消与工具


async def test_cancellation_before_node_and_during_agent_stops(
    client, admin_token, session, seeded_users, tenant_context
):
    await _seed_agent(session)

    cancelled_run = await _create_run(client, admin_token, session, _simple_dsl())
    await RunService(session).cancel(cancelled_run, user=seeded_users[0])
    runner, calls = agent_runner([{"answer": "不该执行"}])
    result = await WorkflowEngine(
        session, cancelled_run, tracer=FakeTracer(), agent_runner=runner
    ).run()
    assert result.status == "cancelled"
    assert calls == []

    active_run_id = await _create_run(client, admin_token, session, _simple_dsl())
    active_run = await session.get(WorkflowRun, active_run_id)

    async def cancel_during_call(_count: int) -> None:
        active_run.status = "cancelled"
        await session.commit()

    runner2, calls2 = agent_runner([{"answer": "迟到的结果"}], on_call=cancel_during_call)
    result2 = await WorkflowEngine(
        session, active_run_id, tracer=FakeTracer(), agent_runner=runner2
    ).run()
    assert result2.status == "cancelled"
    assert len(calls2) == 1
    nodes = await _node_rows(session, active_run_id)
    assert nodes["work"].status != "succeeded"


async def test_tool_node_receives_idempotency_key(
    client, admin_token, session, tenant_context
):
    await _seed_tool(session)
    run_id = await _create_run(
        client, admin_token, session, _simple_dsl(node_type="tool")
    )
    seen: list[tuple[str, dict, Any]] = []

    async def tool_executor(tool_id: str, args: dict, ctx) -> ToolResult:
        seen.append((tool_id, args, ctx))
        return ToolResult(ok=True, data={"echo": args})

    result = await WorkflowEngine(
        session,
        run_id,
        tracer=FakeTracer(),
        tool_executor=tool_executor,
    ).run()

    assert result.status == "succeeded"
    assert seen[0][0] == TOOL_ID
    assert seen[0][1] == {"question": "q", "x": 1}
    assert seen[0][2].idempotency_key == f"{run_id}:work"
    assert result.output == {"echo": {"question": "q", "x": 1}}


# ---------------------------------------------------------------- 观测隔离


async def test_monitoring_failure_is_isolated_from_business_run(
    client, admin_token, session, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(client, admin_token, session, _simple_dsl())
    runner, calls = agent_runner([{"answer": "成功"}])

    result = await WorkflowEngine(
        session,
        run_id,
        tracer=ExplodingSpanTracer(),
        agent_runner=runner,
    ).run()

    assert result.status == "succeeded"
    assert len(calls) == 1
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "succeeded"


async def test_trace_and_root_span_are_persisted_once(
    client, admin_token, session, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(client, admin_token, session, _simple_dsl())
    runner, _ = agent_runner([{"answer": "成功"}])
    tracer = FakeTracer()

    result = await WorkflowEngine(
        session, run_id, tracer=tracer, agent_runner=runner
    ).run()

    assert result.status == "succeeded"
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.trace_id == FakeTrace.trace_id
    assert run.root_span_id == FakeTrace.root_span_id
    assert len(tracer.trace_starts) == 1
    assert {span.name for span in tracer.spans} >= {
        "node.start",
        "node.work",
        "node.end",
    }
    assert tracer.trace.closed == ["succeeded"]


# ------------------------------------------------------- 原子抢占 / 非重试失败


def _start_end_dsl() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [{"from": "start", "to": "end"}],
    }


async def test_atomic_claim_busy_then_recovers_stale_lease(
    client, admin_token, session, tenant_context
):
    """另一 worker 持有新鲜心跳时 busy 让出；心跳过期后可重新抢占。"""
    run_id = await _create_run(client, admin_token, session, _start_end_dsl())
    runner, calls = agent_runner([])

    # 模拟另一个 worker 已持有 start 节点：running + 新鲜心跳。
    nodes = await _node_rows(session, run_id)
    start = nodes["start"]
    now = datetime.now(timezone.utc)
    start.status = "running"
    start.heartbeat_at = now
    start.started_at = now
    await session.commit()

    first_tracer = FakeTracer()
    first = await WorkflowEngine(
        session, run_id, tracer=first_tracer, agent_runner=runner
    ).run()
    assert first.status == "busy"
    assert calls == []

    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "running"
    trace_after_first = run.trace_id
    assert trace_after_first == FakeTrace.trace_id

    # 第二次投递仍是 busy，且复用已落库 trace，不重复开 trace。
    second_tracer = FakeTracer()
    second = await WorkflowEngine(
        session, run_id, tracer=second_tracer, agent_runner=runner
    ).run()
    assert second.status == "busy"
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.trace_id == trace_after_first
    assert second_tracer.trace_starts == []
    assert calls == []

    # 心跳过期后租约可被重新抢占，运行正常完成。
    nodes = await _node_rows(session, run_id)
    nodes["start"].heartbeat_at = now - timedelta(seconds=120)
    await session.commit()

    recovered = await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), agent_runner=runner
    ).run()
    assert recovered.status == "succeeded", recovered.error
    nodes = await _node_rows(session, run_id)
    assert nodes["start"].status == "succeeded"
    assert nodes["end"].status == "succeeded"
    assert calls == []


async def test_non_retryable_tool_error_fails_without_retrying(
    client, admin_token, session, tenant_context
):
    """retryable=False 的工具错误立即失败：只调用一次、attempt 不累加。"""
    await _seed_tool(session)
    # max_retries 省略 -> 工具节点默认 2 次重试预算。
    run_id = await _create_run(
        client, admin_token, session, _simple_dsl(node_type="tool")
    )
    calls: list[tuple[str, dict]] = []

    async def tool_executor(tool_id: str, args: dict, ctx) -> ToolResult:
        calls.append((tool_id, args))
        raise ToolExecutionError(code="invalid_args", message="bad", retryable=False)

    result = await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), tool_executor=tool_executor
    ).run()

    assert result.status == "failed"
    assert result.error == "invalid_args: bad"
    assert len(calls) == 1

    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "failed"
    assert run.error == "invalid_args: bad"

    nodes = await _node_rows(session, run_id)
    assert nodes["work"].status == "failed"
    assert nodes["work"].attempt == 1
    assert nodes["work"].error is not None
    assert nodes["work"].error.startswith("invalid_args")


async def test_list_output_flows_through_run_api(
    client, admin_token, session, tenant_context
):
    """非 dict 输出（list）经 JSONB 落库后能原样通过运行详情 API 返回。"""
    await _seed_agent(session)
    run_id = await _create_run(client, admin_token, session, _simple_dsl())
    runner, calls = agent_runner([[1, 2]])

    result = await WorkflowEngine(
        session, run_id, tracer=FakeTracer(), agent_runner=runner
    ).run()
    assert result.status == "succeeded"
    assert result.output == [1, 2]
    assert len(calls) == 1

    response = await client.get(
        f"/api/v1/runs/{run_id}", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["output"] == [1, 2]
    work = next(node for node in data["nodes"] if node["node_id"] == "work")
    assert work["output"] == [1, 2]
