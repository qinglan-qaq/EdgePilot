"""人工审批 API 集成测试：角色成员资格、冲突、跨租户、审计与恢复入队。"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.audit import AuditLog
from app.models.run import WorkflowRun
from app.workflow.engine import WorkflowEngine
from tests.conftest import TENANT, auth_headers

REVIEW_NODE = "review"


@pytest.fixture
def tenant_context():
    token = set_tenant_id(TENANT)
    yield TENANT
    reset_tenant_id(token)


def _approval_dsl(approvers: list[str]) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": REVIEW_NODE,
                "type": "approval",
                "timeout_ms": 60000,
                "title": "请审批",
                "approvers": approvers,
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": REVIEW_NODE},
            {"from": REVIEW_NODE, "to": "end"},
        ],
    }


async def _create_published_run(client, token: str, dsl: dict) -> int:
    created = await client.post(
        "/api/v1/workflows",
        json={"name": f"wf-approval-{uuid.uuid4().hex[:8]}", "dsl": dsl},
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
        json={"input": {}},
        headers=auth_headers(token),
    )
    assert run.status_code == 200, run.text
    return int(run.json()["data"]["id"])


async def _create_waiting_run(
    client, token: str, session, approvers: list[str]
) -> int:
    run_id = await _create_published_run(client, token, _approval_dsl(approvers))
    result = await WorkflowEngine(session, run_id).run()
    assert result.status == "waiting_approval"
    return run_id


async def test_approver_can_approve_and_audit_and_enqueue_resume(
    client, admin_token, session, seeded_users, tenant_context, monkeypatch
):
    run_id = await _create_waiting_run(
        client, admin_token, session, ["admin"]
    )
    enqueued: list[tuple[int, str | None]] = []

    def fake_enqueue(run_id_: int, resume_node_id: str | None = None) -> bool:
        enqueued.append((run_id_, resume_node_id))
        return True

    monkeypatch.setattr(
        "app.services.run_service.enqueue_workflow_run", fake_enqueue
    )

    response = await client.post(
        f"/api/v1/runs/{run_id}/approve",
        json={"comment": "同意执行"},
        headers=auth_headers(admin_token),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["finished_at"] is None
    review = next(node for node in data["nodes"] if node["node_id"] == REVIEW_NODE)
    assert review["status"] == "succeeded"
    assert review["approval"]["decision"] == "approved"
    assert review["approval"]["comment"] == "同意执行"
    assert review["approval"]["by"] == seeded_users[0].id
    assert enqueued == [(run_id, REVIEW_NODE)]

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "workflow.approve",
            AuditLog.resource_id == run_id,
        )
    )
    assert audit is not None
    assert audit.detail["node_id"] == REVIEW_NODE
    assert audit.detail["decision"] == "approved"
    assert audit.detail["role"] == "admin"


async def test_non_approver_and_admin_not_implicit_are_403(
    client, admin_token, viewer_token, session, tenant_context
):
    run_id = await _create_waiting_run(
        client, admin_token, session, ["operator"]
    )

    as_viewer = await client.post(
        f"/api/v1/runs/{run_id}/approve",
        json={},
        headers=auth_headers(viewer_token),
    )
    assert as_viewer.status_code == 403
    assert as_viewer.json()["code"] == 403

    # admin 不是隐式审批人，必须显式出现在 approvers 中。
    as_admin = await client.post(
        f"/api/v1/runs/{run_id}/approve",
        json={},
        headers=auth_headers(admin_token),
    )
    assert as_admin.status_code == 403

    run = await session.get(WorkflowRun, run_id)
    await session.refresh(run)
    assert run.status == "waiting_approval"


async def test_reject_fails_run_skips_downstream_and_audits(
    client, admin_token, session, tenant_context, monkeypatch
):
    run_id = await _create_waiting_run(
        client, admin_token, session, ["admin"]
    )
    monkeypatch.setattr(
        "app.services.run_service.enqueue_workflow_run",
        lambda *_args, **_kwargs: True,
    )

    response = await client.post(
        f"/api/v1/runs/{run_id}/reject",
        json={"comment": "风险不可接受"},
        headers=auth_headers(admin_token),
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["error"] == "approval_rejected"
    nodes = {node["node_id"]: node for node in data["nodes"]}
    assert nodes[REVIEW_NODE]["status"] == "failed"
    assert nodes[REVIEW_NODE]["error"] == "approval_rejected"
    assert nodes["end"]["status"] == "skipped"

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.action == "workflow.reject",
            AuditLog.resource_id == run_id,
        )
    )
    assert audit is not None
    assert audit.detail["decision"] == "rejected"


async def test_cross_tenant_missing_and_non_waiting_are_controlled_errors(
    client, admin_token, other_tenant_token, session, tenant_context, monkeypatch
):
    waiting_id = await _create_waiting_run(
        client, admin_token, session, ["admin"]
    )
    monkeypatch.setattr(
        "app.services.run_service.enqueue_workflow_run",
        lambda *_args, **_kwargs: True,
    )

    cross = await client.post(
        f"/api/v1/runs/{waiting_id}/approve",
        json={},
        headers=auth_headers(other_tenant_token),
    )
    assert cross.status_code == 404

    missing = await client.post(
        "/api/v1/runs/999999999/approve",
        json={},
        headers=auth_headers(admin_token),
    )
    assert missing.status_code == 404

    pending_id = await _create_published_run(
        client, admin_token, _approval_dsl(["admin"])
    )
    not_waiting = await client.post(
        f"/api/v1/runs/{pending_id}/approve",
        json={},
        headers=auth_headers(admin_token),
    )
    assert not_waiting.status_code == 409
    assert not_waiting.json()["code"] == 409

    first = await client.post(
        f"/api/v1/runs/{waiting_id}/approve",
        json={},
        headers=auth_headers(admin_token),
    )
    assert first.status_code == 200
    second = await client.post(
        f"/api/v1/runs/{waiting_id}/approve",
        json={},
        headers=auth_headers(admin_token),
    )
    assert second.status_code == 409
