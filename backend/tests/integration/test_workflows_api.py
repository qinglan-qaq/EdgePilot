"""工作流持久化、草稿/版本/发布/校验、运行记录与计划 API 集成测试。

覆盖 Task 22 前置部分：权限矩阵、跨租户 404、校验报告、发布不可变版本、
运行创建预插节点、列表过滤、日志时间线、取消状态机、last_run 汇总与审计。
运行创建不触发执行，不做任何伪造；所有 session 绑定外层事务统一回滚。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.run import WorkflowNodeRun, WorkflowRun
from app.models.user import User
from tests.conftest import (
    PASSWORD,
    TENANT,
    auth_headers,
    login_token,
)

OPERATOR_EMAIL = "operator@edgepilot.local"
AGENT_NAME = "wf-publish-agent"


@pytest.fixture
async def operator_token(client, session, seeded_users):
    session.add(
        User(
            tenant_id=TENANT,
            email=OPERATOR_EMAIL,
            password_hash=hash_password(PASSWORD),
            display_name="Operator",
            role="operator",
            status="active",
        )
    )
    await session.flush()
    return await login_token(client, OPERATOR_EMAIL)


async def _seed_agent(session, name: str = AGENT_NAME) -> None:
    session.add(Agent(tenant_id=TENANT, name=name, agent_type="device", status="active"))
    await session.flush()


def _dsl(agent_name: str = AGENT_NAME, marker: str = "v1") -> dict:
    """声明顺序刻意不是字母序，用于断言节点预插顺序。"""
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "zeta",
                "type": "agent",
                "agent": agent_name,
                "timeout_ms": 30000,
                "input_map": {"question": "$input.question", "marker": marker},
            },
            {"id": "alpha", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "zeta"},
            {"from": "zeta", "to": "alpha"},
        ],
    }


async def _create_workflow(client, token: str, *, name: str, dsl: dict | None = None) -> dict:
    response = await client.post(
        "/api/v1/workflows",
        json={"name": name, "description": "demo", "dsl": dsl},
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _publish(client, token: str, workflow_id: int) -> dict:
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", headers=auth_headers(token)
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _create_run(
    client, token: str, workflow_id: int, *, input_data: dict | None = None, trigger="manual"
) -> dict:
    response = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"input": input_data, "trigger_type": trigger},
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ------------------------------------------------------------------ CRUD / draft


async def test_workflow_crud_and_draft_edit(client, admin_token):
    created = await _create_workflow(client, admin_token, name="wf-crud", dsl=_dsl())
    assert created["status"] == "draft"
    assert created["current_version"] == 0
    assert created["draft_version"] == 0
    assert created["last_run"] is None
    assert [node["id"] for node in created["dsl"]["nodes"]] == ["start", "zeta", "alpha"]
    workflow_id = created["id"]

    detail = (
        await client.get(
            f"/api/v1/workflows/{workflow_id}", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert detail["id"] == workflow_id
    assert detail["dsl"]["nodes"][1]["id"] == "zeta"

    patched = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        json={"description": "changed", "dsl": _dsl(marker="draft")},
        headers=auth_headers(admin_token),
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()["data"]
    assert body["description"] == "changed"
    assert body["dsl"]["nodes"][1]["input_map"]["marker"] == "draft"

    versions = (
        await client.get(
            f"/api/v1/workflows/{workflow_id}/versions",
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    assert versions == []

    listing = (
        await client.get("/api/v1/workflows", headers=auth_headers(admin_token))
    ).json()["data"]
    row = next(item for item in listing["items"] if item["id"] == workflow_id)
    assert row["name"] == "wf-crud"
    assert row["last_run"] is None
    assert "dsl" not in row

    duplicate = await client.post(
        "/api/v1/workflows",
        json={"name": "wf-crud"},
        headers=auth_headers(admin_token),
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == 409


async def test_workflow_create_without_dsl_uses_empty_draft(client, admin_token):
    created = await _create_workflow(client, admin_token, name="wf-empty")
    assert created["dsl"] == {"nodes": [], "edges": []}
    assert created["draft_version"] == 0


# ---------------------------------------------------------------------- 权限


async def test_viewer_is_read_only(client, admin_token, viewer_token):
    created = await _create_workflow(client, admin_token, name="wf-viewer")
    workflow_id = created["id"]

    assert (
        await client.get("/api/v1/workflows", headers=auth_headers(viewer_token))
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/workflows/{workflow_id}", headers=auth_headers(viewer_token)
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/api/v1/workflows/{workflow_id}/versions",
            headers=auth_headers(viewer_token),
        )
    ).status_code == 200
    validate = await client.post(
        f"/api/v1/workflows/{workflow_id}/validate", headers=auth_headers(viewer_token)
    )
    assert validate.status_code == 200
    assert validate.json()["data"]["valid"] is False

    create = await client.post(
        "/api/v1/workflows", json={"name": "viewer-made"}, headers=auth_headers(viewer_token)
    )
    assert create.status_code == 403
    patch = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        json={"name": "hijack"},
        headers=auth_headers(viewer_token),
    )
    assert patch.status_code == 403
    publish = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", headers=auth_headers(viewer_token)
    )
    assert publish.status_code == 403
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={},
        headers=auth_headers(viewer_token),
    )
    assert run.status_code == 403
    cancel = await client.post(
        "/api/v1/runs/1/cancel", headers=auth_headers(viewer_token)
    )
    assert cancel.status_code == 403


async def test_operator_can_write_and_run_but_not_publish(client, operator_token, session):
    await _seed_agent(session)
    created = await _create_workflow(client, operator_token, name="wf-operator", dsl=_dsl())
    workflow_id = created["id"]

    patched = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        json={"description": "operator edit"},
        headers=auth_headers(operator_token),
    )
    assert patched.status_code == 200

    publish = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish",
        headers=auth_headers(operator_token),
    )
    assert publish.status_code == 403

    # 尚未发布：operator 有触发权限但业务上 409
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={},
        headers=auth_headers(operator_token),
    )
    assert run.status_code == 409


async def test_cross_tenant_everything_is_404(
    client, admin_token, other_tenant_token, session
):
    await _seed_agent(session)
    created = await _create_workflow(client, admin_token, name="tenant-a-wf", dsl=_dsl())
    workflow_id = created["id"]
    await _publish(client, admin_token, workflow_id)
    run = await _create_run(client, admin_token, workflow_id)
    run_id = run["id"]
    other = auth_headers(other_tenant_token)

    requests = [
        client.get(f"/api/v1/workflows/{workflow_id}", headers=other),
        client.patch(
            f"/api/v1/workflows/{workflow_id}", json={"name": "x"}, headers=other
        ),
        client.post(f"/api/v1/workflows/{workflow_id}/validate", headers=other),
        client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=other),
        client.get(f"/api/v1/workflows/{workflow_id}/versions", headers=other),
        client.post(f"/api/v1/workflows/{workflow_id}/runs", json={}, headers=other),
        client.get(f"/api/v1/runs/{run_id}", headers=other),
        client.get(f"/api/v1/runs/{run_id}/logs", headers=other),
        client.post(f"/api/v1/runs/{run_id}/cancel", headers=other),
    ]
    responses = [await request for request in requests]
    assert [response.status_code for response in responses] == [404] * len(requests)
    assert all(response.json()["code"] == 404 for response in responses)

    other_list = (await client.get("/api/v1/workflows", headers=other)).json()["data"]
    assert all(item["id"] != workflow_id for item in other_list["items"])
    other_runs = (await client.get("/api/v1/runs", headers=other)).json()["data"]
    assert other_runs["total"] == 0


# -------------------------------------------------------------------- 校验


async def test_validate_reports_are_200_and_cover_key_error_cases(
    client, admin_token, session
):
    await _seed_agent(session)
    created = await _create_workflow(client, admin_token, name="wf-validate")
    workflow_id = created["id"]
    url = f"/api/v1/workflows/{workflow_id}/validate"

    empty = await client.post(url, headers=auth_headers(admin_token))
    assert empty.status_code == 200
    assert empty.json()["data"]["valid"] is False
    assert "nodes_empty" in {item["code"] for item in empty.json()["data"]["errors"]}

    valid = await client.post(
        url, json={"dsl": _dsl()}, headers=auth_headers(admin_token)
    )
    assert valid.status_code == 200
    assert valid.json()["data"]["valid"] is True
    assert valid.json()["data"]["topological_order"] == ["start", "zeta", "alpha"]

    unknown_agent = await client.post(
        url, json={"dsl": _dsl(agent_name="missing-agent")}, headers=auth_headers(admin_token)
    )
    codes = {item["code"] for item in unknown_agent.json()["data"]["errors"]}
    assert unknown_agent.status_code == 200
    assert unknown_agent.json()["data"]["valid"] is False
    assert "agent_not_found_or_disabled" in codes

    cycle_dsl = {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {"id": "a", "type": "tool", "tool": "x", "timeout_ms": 1000},
            {"id": "b", "type": "tool", "tool": "x", "timeout_ms": 1000},
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "a"},
            {"from": "a", "to": "b"},
            {"from": "b", "to": "a"},
            {"from": "b", "to": "end"},
        ],
    }
    cycle = await client.post(
        url, json={"dsl": cycle_dsl}, headers=auth_headers(admin_token)
    )
    assert cycle.status_code == 200
    assert "cycle_detected" in {item["code"] for item in cycle.json()["data"]["errors"]}

    bad_expression = {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "ask",
                "type": "agent",
                "agent": AGENT_NAME,
                "timeout_ms": 30000,
                "input_map": {"question": "$input["},
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "ask"},
            {"from": "ask", "to": "end"},
        ],
    }
    expr = await client.post(
        url, json={"dsl": bad_expression}, headers=auth_headers(admin_token)
    )
    assert expr.status_code == 200
    assert "invalid_input_map_expression" in {
        item["code"] for item in expr.json()["data"]["errors"]
    }


# ---------------------------------------------------------------- 发布 / 版本


async def test_publish_creates_immutable_versions_and_draft_stays_editable(
    client, admin_token, session
):
    await _seed_agent(session)
    created = await _create_workflow(client, admin_token, name="wf-publish", dsl=_dsl())
    workflow_id = created["id"]

    v1 = await _publish(client, admin_token, workflow_id)
    assert v1["version"] == 1
    assert v1["dsl"]["nodes"][1]["input_map"]["marker"] == "v1"

    detail = (
        await client.get(
            f"/api/v1/workflows/{workflow_id}", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert detail["status"] == "published"
    assert detail["current_version"] == 1
    assert detail["draft_version"] == 0

    # 改草稿：详情立刻反映草稿，但 v1 快照与运行使用的 current_version 不变
    patched = await client.patch(
        f"/api/v1/workflows/{workflow_id}",
        json={"dsl": _dsl(marker="v2")},
        headers=auth_headers(admin_token),
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["dsl"]["nodes"][1]["input_map"]["marker"] == "v2"

    versions = (
        await client.get(
            f"/api/v1/workflows/{workflow_id}/versions",
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    assert [row["version"] for row in versions] == [1]
    assert versions[0]["dsl"]["nodes"][1]["input_map"]["marker"] == "v1"

    v2 = await _publish(client, admin_token, workflow_id)
    assert v2["version"] == 2
    versions = (
        await client.get(
            f"/api/v1/workflows/{workflow_id}/versions",
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    assert [row["version"] for row in versions] == [2, 1]
    assert versions[0]["dsl"]["nodes"][1]["input_map"]["marker"] == "v2"
    assert versions[1]["dsl"]["nodes"][1]["input_map"]["marker"] == "v1"
    assert versions[0]["dsl_hash"] != versions[1]["dsl_hash"]
    assert detail["current_version"] == 1  # 第二次发布前抓取的详情


async def test_publish_invalid_draft_returns_400_with_error_detail(
    client, admin_token
):
    created = await _create_workflow(client, admin_token, name="wf-bad-publish")
    publish = await client.post(
        f"/api/v1/workflows/{created['id']}/publish",
        headers=auth_headers(admin_token),
    )
    assert publish.status_code == 400
    body = publish.json()
    assert body["code"] == 400
    assert body["data"]["errors"]
    assert "nodes_empty" in {item["code"] for item in body["data"]["errors"]}

    detail = (
        await client.get(
            f"/api/v1/workflows/{created['id']}", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert detail["status"] == "draft"
    assert detail["current_version"] == 0


# ------------------------------------------------------------------- 运行


async def test_run_creation_persists_pending_rows_without_execution(
    client, admin_token, session
):
    await _seed_agent(session)
    created = await _create_workflow(client, admin_token, name="wf-run", dsl=_dsl())
    workflow_id = created["id"]

    unpublished = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={},
        headers=auth_headers(admin_token),
    )
    assert unpublished.status_code == 409

    version = await _publish(client, admin_token, workflow_id)
    run = await _create_run(
        client, admin_token, workflow_id, input_data={"question": "设备是否在线"}
    )

    assert run["status"] == "pending"
    assert run["trace_id"] is None
    assert run["started_at"] is None
    assert run["finished_at"] is None
    assert run["workflow_id"] == workflow_id
    assert run["workflow_name"] == "wf-run"
    assert run["workflow_version_id"] == version["id"]
    assert run["workflow_version"] == 1
    assert run["trigger_type"] == "manual"
    assert run["input"] == {"question": "设备是否在线"}
    assert run["root_span_id"] is None
    assert [node["node_id"] for node in run["nodes"]] == ["start", "zeta", "alpha"]
    assert all(node["status"] == "pending" for node in run["nodes"])
    assert all(node["attempt"] == 1 for node in run["nodes"])
    assert all(node["started_at"] is None for node in run["nodes"])
    assert all(node["span_id"] is None for node in run["nodes"])
    assert all(node["heartbeat_at"] is None for node in run["nodes"])

    db_rows = (
        await session.scalars(
            select(WorkflowNodeRun)
            .where(WorkflowNodeRun.run_id == run["id"])
            .order_by(WorkflowNodeRun.id)
        )
    ).all()
    assert [row.node_id for row in db_rows] == ["start", "zeta", "alpha"]
    db_run = await session.get(WorkflowRun, run["id"])
    assert db_run.status == "pending"
    assert db_run.trace_id is None

    detail = (
        await client.get(f"/api/v1/runs/{run['id']}", headers=auth_headers(admin_token))
    ).json()["data"]
    assert [node["node_id"] for node in detail["nodes"]] == ["start", "zeta", "alpha"]

    logs = (
        await client.get(
            f"/api/v1/runs/{run['id']}/logs", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert len(logs) == 3
    assert set(logs[0]) == {
        "node_id",
        "node_type",
        "status",
        "attempt",
        "started_at",
        "finished_at",
        "error",
    }
    assert logs[1]["node_type"] == "agent"

    # last_run 汇总在详情与列表中都指向这次 pending 运行
    wf_detail = (
        await client.get(
            f"/api/v1/workflows/{workflow_id}", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert wf_detail["last_run"]["id"] == run["id"]
    assert wf_detail["last_run"]["status"] == "pending"
    listing = (
        await client.get("/api/v1/workflows", headers=auth_headers(admin_token))
    ).json()["data"]
    listed = next(item for item in listing["items"] if item["id"] == workflow_id)
    assert listed["last_run"]["id"] == run["id"]


async def test_run_list_filters(client, admin_token, session):
    await _seed_agent(session)
    first = await _create_workflow(client, admin_token, name="wf-list-a", dsl=_dsl())
    second = await _create_workflow(client, admin_token, name="wf-list-b", dsl=_dsl())
    await _publish(client, admin_token, first["id"])
    await _publish(client, admin_token, second["id"])

    run_a = await _create_run(client, admin_token, first["id"])
    run_b = await _create_run(client, admin_token, second["id"])
    await client.post(
        f"/api/v1/runs/{run_b['id']}/cancel", headers=auth_headers(admin_token)
    )

    by_workflow = (
        await client.get(
            f"/api/v1/runs?workflow_id={first['id']}", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert by_workflow["total"] == 1
    assert by_workflow["items"][0]["id"] == run_a["id"]
    assert by_workflow["items"][0]["workflow_name"] == "wf-list-a"
    assert by_workflow["items"][0]["workflow_version"] == 1

    pending = (
        await client.get("/api/v1/runs?status=pending", headers=auth_headers(admin_token))
    ).json()["data"]
    assert {item["id"] for item in pending["items"]} == {run_a["id"]}

    cancelled = (
        await client.get(
            "/api/v1/runs?status=cancelled", headers=auth_headers(admin_token)
        )
    ).json()["data"]
    assert {item["id"] for item in cancelled["items"]} == {run_b["id"]}

    missing = (
        await client.get("/api/v1/runs?workflow_id=999999", headers=auth_headers(admin_token))
    ).json()["data"]
    assert missing["total"] == 0


async def test_cancel_state_machine(client, admin_token, session):
    await _seed_agent(session)
    created = await _create_workflow(client, admin_token, name="wf-cancel", dsl=_dsl())
    await _publish(client, admin_token, created["id"])
    run = await _create_run(client, admin_token, created["id"])

    # 模拟执行引擎已推进：run=running，第一个节点成功
    db_run = await session.get(WorkflowRun, run["id"])
    db_run.status = "running"
    first_node = (
        await session.scalars(
            select(WorkflowNodeRun)
            .where(WorkflowNodeRun.run_id == run["id"])
            .order_by(WorkflowNodeRun.id)
        )
    ).all()[0]
    first_node.status = "succeeded"
    await session.flush()

    cancelled = await client.post(
        f"/api/v1/runs/{run['id']}/cancel", headers=auth_headers(admin_token)
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()["data"]
    assert body["status"] == "cancelled"
    assert body["finished_at"] is not None
    statuses = {node["node_id"]: node["status"] for node in body["nodes"]}
    assert statuses == {"start": "succeeded", "zeta": "skipped", "alpha": "skipped"}

    again = await client.post(
        f"/api/v1/runs/{run['id']}/cancel", headers=auth_headers(admin_token)
    )
    assert again.status_code == 409
    assert again.json()["code"] == 409


# ------------------------------------------------------------------ 审计 / 404


async def test_audit_records_publish_run_and_cancel(client, admin_token, session):
    await _seed_agent(session)
    created = await _create_workflow(client, admin_token, name="wf-audit", dsl=_dsl())
    await _publish(client, admin_token, created["id"])
    run = await _create_run(client, admin_token, created["id"])
    await client.post(
        f"/api/v1/runs/{run['id']}/cancel", headers=auth_headers(admin_token)
    )

    rows = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == TENANT,
                AuditLog.action.in_(
                    ["workflow.publish", "workflow.run", "workflow.cancel"]
                ),
                AuditLog.resource_id.in_([created["id"], run["id"]]),
            )
        )
    ).all()
    actions = {row.action for row in rows}
    assert {"workflow.publish", "workflow.run", "workflow.cancel"} <= actions
    publish_row = next(row for row in rows if row.action == "workflow.publish")
    run_row = next(row for row in rows if row.action == "workflow.run")
    cancel_row = next(row for row in rows if row.action == "workflow.cancel")
    assert publish_row.detail["version"] == 1
    assert run_row.detail["run_id"] == run["id"]
    assert cancel_row.detail["from_status"] == "pending"


async def test_missing_resources_return_404(client, admin_token):
    headers = auth_headers(admin_token)
    calls = [
        client.get("/api/v1/workflows/999999", headers=headers),
        client.patch("/api/v1/workflows/999999", json={"name": "x"}, headers=headers),
        client.post("/api/v1/workflows/999999/validate", headers=headers),
        client.post("/api/v1/workflows/999999/publish", headers=headers),
        client.get("/api/v1/workflows/999999/versions", headers=headers),
        client.post("/api/v1/workflows/999999/runs", json={}, headers=headers),
        client.get("/api/v1/runs/999999", headers=headers),
        client.get("/api/v1/runs/999999/logs", headers=headers),
        client.post("/api/v1/runs/999999/cancel", headers=headers),
    ]
    responses = [await call for call in calls]
    assert [response.status_code for response in responses] == [404] * len(calls)
