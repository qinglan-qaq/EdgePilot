"""Agent CRUD、不可变版本发布与 Prompt 引用的集成测试。

需要真实数据库：唯一约束、行锁发布事务与租户过滤都依赖 DB 行为。
所有 session 绑定外层事务，测试结束统一回滚。
"""

import hashlib
import json

import pytest
from sqlalchemy import select

from app.models.run import AgentRun
from tests.conftest import TENANT, auth_headers, login_token


async def _create_agent(client, token: str, **overrides) -> dict:
    payload = {
        "name": "vision-a",
        "agent_type": "vision",
        "prompt_template_id": None,
        "prompt_version": None,
        "config": {},
    }
    payload.update(overrides)
    response = await client.post(
        "/api/v1/agents", json=payload, headers=auth_headers(token)
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _create_published_prompt(client, token: str) -> dict:
    """创建模板 + v1 并发布，返回模板详情（含 id 与版本内容）。"""
    template = (
        await client.post(
            "/api/v1/observability/prompts",
            headers=auth_headers(token),
            json={
                "template_id": "agent.vision",
                "template_name": "Vision Agent",
                "category": "agent",
            },
        )
    ).json()["data"]
    version = (
        await client.post(
            f"/api/v1/observability/prompts/{template['id']}/versions",
            headers=auth_headers(token),
            json={
                "system_prompt": "你是视觉事件分析助手",
                "user_prompt_template": "任务：{{task}}",
                "variables_schema": {
                    "type": "object",
                    "required": ["task"],
                    "properties": {"task": {"type": "string"}},
                },
            },
        )
    ).json()["data"]
    published = await client.post(
        f"/api/v1/observability/prompts/{template['id']}/publish",
        headers=auth_headers(token),
        json={"version": version["version"]},
    )
    assert published.status_code == 200, published.text
    template["version_row"] = version
    return template


def _rendered_hash(version_row: dict) -> str:
    """与 service 的 canonical JSON 口径一致。"""
    canonical = json.dumps(
        {
            "system_prompt": version_row["system_prompt"],
            "user_prompt_template": version_row["user_prompt_template"],
            "variables_schema": version_row["variables_schema"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_publish_creates_immutable_version_and_bumps_current(client, admin_token):
    payload = {"name": "vision-a", "agent_type": "vision", "prompt_template_id": None,
               "prompt_version": None, "config": {}}
    created = (await client.post("/api/v1/agents", json=payload,
                                 headers=auth_headers(admin_token))).json()["data"]
    r1 = await client.post(f"/api/v1/agents/{created['id']}/publish",
                           headers=auth_headers(admin_token))
    assert r1.json()["data"]["version"] == 1
    r2 = await client.post(f"/api/v1/agents/{created['id']}/publish",
                           headers=auth_headers(admin_token))
    assert r2.json()["data"]["version"] == 2


@pytest.mark.asyncio
async def test_agent_version_rows_are_not_updatable(client, admin_token):
    # 应用层禁止 UPDATE agent_versions：调用 PATCH 不存在该路由 → 405
    r = await client.patch("/api/v1/agents/1/versions/1", headers=auth_headers(admin_token))
    assert r.status_code in (404, 405)


@pytest.mark.asyncio
async def test_viewer_cannot_publish_agent(client, seeded_users):
    token = await login_token(client, "viewer@edgepilot.local")
    r = await client.post("/api/v1/agents/1/publish", headers=auth_headers(token))
    assert r.status_code == 403
    assert r.json()["code"] == 403


@pytest.mark.asyncio
async def test_cross_tenant_returns_404(client, admin_token, other_tenant_token):
    created = await _create_agent(client, admin_token, name="tenant-a-only")
    agent_id = created["id"]

    get_r = await client.get(
        f"/api/v1/agents/{agent_id}", headers=auth_headers(other_tenant_token)
    )
    assert get_r.status_code == 404
    assert get_r.json()["code"] == 404

    patch_r = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers(other_tenant_token),
        json={"name": "hijacked"},
    )
    assert patch_r.status_code == 404

    publish_r = await client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=auth_headers(other_tenant_token),
    )
    assert publish_r.status_code == 404

    versions_r = await client.get(
        f"/api/v1/agents/{agent_id}/versions",
        headers=auth_headers(other_tenant_token),
    )
    assert versions_r.status_code == 404


@pytest.mark.asyncio
async def test_publish_snapshots_prompt_reference(client, admin_token):
    template = await _create_published_prompt(client, admin_token)
    created = await _create_agent(
        client,
        admin_token,
        name="vision-with-prompt",
        prompt_template_id=template["id"],
        prompt_version=None,
    )

    published = await client.post(
        f"/api/v1/agents/{created['id']}/publish",
        headers=auth_headers(admin_token),
    )
    assert published.status_code == 200, published.text
    snapshot = published.json()["data"]["snapshot"]
    assert snapshot["prompt"] == {
        "template_id": template["id"],
        "version": template["version_row"]["version"],
        "rendered_hash": _rendered_hash(template["version_row"]),
    }
    # 快照只存引用与哈希，不存 Prompt 全文
    assert "你是视觉事件分析助手" not in json.dumps(snapshot, ensure_ascii=False)


@pytest.mark.asyncio
async def test_publish_with_missing_prompt_version_returns_409(client, admin_token):
    template = await _create_published_prompt(client, admin_token)
    created = await _create_agent(
        client,
        admin_token,
        name="vision-bad-prompt",
        prompt_template_id=template["id"],
        prompt_version=99,
    )
    r = await client.post(
        f"/api/v1/agents/{created['id']}/publish",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 409

    missing_template = await _create_agent(
        client,
        admin_token,
        name="vision-missing-template",
        prompt_template_id=999999,
    )
    r2 = await client.post(
        f"/api/v1/agents/{missing_template['id']}/publish",
        headers=auth_headers(admin_token),
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_list_versions_newest_first(client, admin_token):
    created = await _create_agent(client, admin_token, name="vision-history")
    for _ in range(3):
        await client.post(
            f"/api/v1/agents/{created['id']}/publish",
            headers=auth_headers(admin_token),
        )

    r = await client.get(
        f"/api/v1/agents/{created['id']}/versions",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    versions = r.json()["data"]
    assert [row["version"] for row in versions] == [3, 2, 1]
    assert all(row["snapshot"]["name"] == "vision-history" for row in versions)
    assert all(row["snapshot"]["prompt"] is None for row in versions)


@pytest.mark.asyncio
async def test_list_runs_only_returns_that_agents_runs(client, admin_token, session):
    first = await _create_agent(client, admin_token, name="vision-runs-a")
    second = await _create_agent(client, admin_token, name="vision-runs-b")
    session.add_all(
        [
            AgentRun(
                tenant_id=TENANT,
                agent_id=first["id"],
                agent_version=1,
                status="succeeded",
                output={"answer": "ok"},
            ),
            AgentRun(
                tenant_id=TENANT,
                agent_id=second["id"],
                agent_version=1,
                status="failed",
                error="agent_output_invalid: schema",
            ),
        ]
    )
    await session.flush()

    r = await client.get(
        f"/api/v1/agents/{first['id']}/runs",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert [row["agent_id"] for row in data["items"]] == [first["id"]]
    assert data["items"][0]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_delete_agent_with_runs_returns_409(client, admin_token, session):
    created = await _create_agent(client, admin_token, name="vision-with-runs")
    session.add(
        AgentRun(
            tenant_id=TENANT,
            agent_id=created["id"],
            agent_version=1,
            status="succeeded",
        )
    )
    await session.flush()

    r = await client.delete(
        f"/api/v1/agents/{created['id']}", headers=auth_headers(admin_token)
    )
    assert r.status_code == 409
    still_there = await session.scalar(
        select(AgentRun.id).where(AgentRun.agent_id == created["id"])
    )
    assert still_there is not None
