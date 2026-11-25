"""工作流超时与运行回收测试：节点超时、审批过期、stale 运行发现。

全部使用真实数据库事务；节点超时使用很短的 ``asyncio.wait_for``，
不等待任何真实网络。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.base import AgentRunResult
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.run import WorkflowNodeRun, WorkflowRun
from app.services.run_service import expire_waiting_approvals, find_stale_runs
from app.workflow.engine import WorkflowEngine
from tests.conftest import TENANT, auth_headers

AGENT_NAME = "timeout-agent"


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
        json={"name": f"wf-timeout-{uuid.uuid4().hex[:8]}", "dsl": dsl},
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


def _agent_dsl(*, node_timeout_ms: int = 10, max_retries: int = 0) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "slow",
                "type": "agent",
                "agent": AGENT_NAME,
                "timeout_ms": node_timeout_ms,
                "max_retries": max_retries,
                "input_map": {"question": "$input.question"},
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "slow"},
            {"from": "slow", "to": "end"},
        ],
    }


def _approval_dsl() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "review",
                "type": "approval",
                "timeout_ms": 60000,
                "title": "审批",
                "approvers": ["admin"],
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "review"},
            {"from": "review", "to": "end"},
        ],
    }


def _success_agent_result() -> AgentRunResult:
    return AgentRunResult(
        agent_run_id=1,
        status="succeeded",
        output={"answer": "ok"},
        error=None,
        trace_id=None,
        span_id=None,
        total_tokens=None,
        total_cost=None,
        retry_count=0,
    )


async def test_node_timeout_marks_node_and_run_failed(
    client, admin_token, session, tenant_context
):
    await _seed_agent(session)
    run_id = await _create_run(client, admin_token, session, _agent_dsl())

    async def slow_runner(*_args, **_kwargs):
        await asyncio.sleep(0.2)
        return _success_agent_result()

    result = await WorkflowEngine(
        session, run_id, agent_runner=slow_runner
    ).run()

    assert result.status == "failed"
    assert result.error == "timeout: 节点执行超过 10ms"
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "failed"
    assert run.error == "timeout: 节点执行超过 10ms"
    assert run.finished_at is not None
    node = await session.scalar(
        select(WorkflowNodeRun).where(
            WorkflowNodeRun.run_id == run_id,
            WorkflowNodeRun.node_id == "slow",
        )
    )
    await session.refresh(node)
    assert node.status == "failed"
    assert node.error == "timeout: 节点执行超过 10ms"
    assert node.finished_at is not None


async def test_approval_not_expired_before_expires_at_is_untouched(
    client, admin_token, session, tenant_context
):
    """审批尚未到期时扫描为空操作，运行保持 waiting_approval。"""
    run_id = await _create_run(client, admin_token, session, _approval_dsl())
    result = await WorkflowEngine(session, run_id).run()
    assert result.status == "waiting_approval"

    now = datetime.now(timezone.utc)
    expired = await expire_waiting_approvals(
        session, now=now - timedelta(seconds=1)
    )

    assert expired == []
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "waiting_approval"
    nodes = (
        await session.scalars(
            select(WorkflowNodeRun).where(WorkflowNodeRun.run_id == run_id)
        )
    ).all()
    review = next(node for node in nodes if node.node_id == "review")
    assert review.status == "waiting_approval"


async def test_expired_approval_fails_run_and_writes_audit(
    client, admin_token, session, tenant_context
):
    run_id = await _create_run(client, admin_token, session, _approval_dsl())
    result = await WorkflowEngine(session, run_id).run()
    assert result.status == "waiting_approval"

    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    expired = await expire_waiting_approvals(session, now=now)

    assert expired == [run_id]
    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "failed"
    assert run.error == "approval_timeout"
    assert run.finished_at is not None
    nodes = (
        await session.scalars(
            select(WorkflowNodeRun).where(WorkflowNodeRun.run_id == run_id)
        )
    ).all()
    statuses = {node.node_id: node for node in nodes}
    assert statuses["review"].status == "failed"
    assert statuses["review"].error == "approval_timeout"
    assert statuses["end"].status == "skipped"

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "workflow.approval_timeout",
            AuditLog.resource_id == run_id,
        )
    )
    assert audit is not None
    assert audit.detail["decision"] == "timeout"


async def test_find_stale_runs_returns_old_and_excludes_fresh(
    session, tenant_context
):
    now = datetime.now(timezone.utc)
    old = now - timedelta(seconds=120)

    old_run = WorkflowRun(
        tenant_id=TENANT,
        workflow_version_id=1,
        status="running",
        started_at=old,
    )
    fresh_run = WorkflowRun(
        tenant_id=TENANT,
        workflow_version_id=1,
        status="running",
        started_at=now,
    )
    session.add_all([old_run, fresh_run])
    await session.flush()

    session.add_all(
        [
            WorkflowNodeRun(
                tenant_id=TENANT,
                run_id=old_run.id,
                node_id="old_node",
                node_type="agent",
                status="running",
                heartbeat_at=old,
            ),
            WorkflowNodeRun(
                tenant_id=TENANT,
                run_id=fresh_run.id,
                node_id="fresh_node",
                node_type="agent",
                status="running",
                heartbeat_at=now,
            ),
        ]
    )
    await session.flush()

    stale = await find_stale_runs(session, older_than_s=60, now=now)

    assert stale == [old_run.id]
    assert fresh_run.id not in stale
