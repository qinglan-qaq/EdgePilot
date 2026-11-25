"""Prompt 执行历史 API 集成测试（Task 17）。

覆盖：倒序分页、字段完整、跨租户 404、未知模板 404、无记录空页。
数据直接写 SQL 并共享外层事务，不 commit / rollback。
"""

import json

import pytest
from sqlalchemy import text

from tests.conftest import OTHER_TENANT, TENANT, auth_headers

BASE = "/api/v1/observability/prompts"
EXECUTION_FIELDS = {
    "id",
    "trace_id",
    "span_id",
    "template_version",
    "render_status",
    "model_name",
    "prompt_hash",
    "variables",
    "rendered_messages",
    "created_at",
}


async def _insert_template(
    session, *, tenant_id, template_id: str, template_name: str = "测试模板"
) -> int:
    row = await session.execute(
        text(
            "INSERT INTO prompt_templates"
            " (tenant_id, template_id, template_name, status)"
            " VALUES (:tenant_id, :template_id, :template_name, 'active')"
            " RETURNING id"
        ),
        {
            "tenant_id": tenant_id,
            "template_id": template_id,
            "template_name": template_name,
        },
    )
    return int(row.scalar_one())


async def _insert_execution(
    session,
    *,
    tenant_id,
    template_pk: int,
    version: int,
    render_status: str,
    created_at_sql: str,
    trace_id: str | None = "a" * 32,
    span_id: str | None = "b" * 16,
    model_name: str | None = "mock-model",
    prompt_hash: str | None = "f" * 64,
    variables: dict | None = None,
    messages: list[dict] | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO prompt_executions"
            " (tenant_id, trace_id, span_id, template_id, template_version,"
            " variables, rendered_messages, prompt_hash, render_status, model_name,"
            " created_at)"
            " VALUES (:tenant_id, :trace_id, :span_id, :template_id, :version,"
            " CAST(:variables AS jsonb), CAST(:messages AS jsonb), :prompt_hash,"
            " :render_status, :model_name, " + created_at_sql + ")"
        ),
        {
            "tenant_id": tenant_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "template_id": template_pk,
            "version": version,
            "variables": json.dumps(variables or {}, ensure_ascii=False),
            "messages": json.dumps(messages or [], ensure_ascii=False),
            "prompt_hash": prompt_hash,
            "render_status": render_status,
            "model_name": model_name,
        },
    )


@pytest.mark.asyncio
async def test_executions_sorted_desc_and_paginated(client, viewer_token, session):
    template_pk = await _insert_template(
        session, tenant_id=TENANT, template_id="tpl_exec_order"
    )
    await _insert_execution(
        session,
        tenant_id=TENANT,
        template_pk=template_pk,
        version=1,
        render_status="ok",
        created_at_sql="now() - interval '1 hour'",
        trace_id="a" * 32,
        span_id="b" * 16,
        variables={"question": "旧"},
        messages=[{"role": "user", "content": "旧"}],
    )
    await _insert_execution(
        session,
        tenant_id=TENANT,
        template_pk=template_pk,
        version=2,
        render_status="schema_error",
        created_at_sql="now()",
        trace_id="c" * 32,
        span_id="d" * 16,
        variables={"question": "新"},
        messages=[{"role": "user", "content": "新"}],
    )
    # 同租户其它模板的执行不得混入。
    other_template_pk = await _insert_template(
        session, tenant_id=TENANT, template_id="tpl_exec_other"
    )
    await _insert_execution(
        session,
        tenant_id=TENANT,
        template_pk=other_template_pk,
        version=9,
        render_status="ok",
        created_at_sql="now()",
    )

    response = await client.get(
        f"{BASE}/{template_pk}/executions",
        params={"page": 1, "size": 10},
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["size"] == 10
    assert [item["template_version"] for item in data["items"]] == [2, 1]

    newest, oldest = data["items"]
    assert set(newest) == EXECUTION_FIELDS
    assert newest["trace_id"] == "c" * 32
    assert newest["span_id"] == "d" * 16
    assert newest["render_status"] == "schema_error"
    assert newest["model_name"] == "mock-model"
    assert newest["prompt_hash"] == "f" * 64
    assert newest["variables"] == {"question": "新"}
    assert newest["rendered_messages"][0]["content"] == "新"
    assert newest["created_at"] >= oldest["created_at"]

    # 分页：size=1 时第 1 页是新记录，第 2 页是旧记录。
    first = await client.get(
        f"{BASE}/{template_pk}/executions",
        params={"page": 1, "size": 1},
        headers=auth_headers(viewer_token),
    )
    second = await client.get(
        f"{BASE}/{template_pk}/executions",
        params={"page": 2, "size": 1},
        headers=auth_headers(viewer_token),
    )
    assert first.json()["data"]["total"] == 2
    assert first.json()["data"]["items"][0]["template_version"] == 2
    assert second.json()["data"]["items"][0]["template_version"] == 1


@pytest.mark.asyncio
async def test_executions_cross_tenant_and_unknown_are_404(
    client, viewer_token, other_tenant_token, session
):
    our_template_pk = await _insert_template(
        session, tenant_id=TENANT, template_id="tpl_exec_tenant_a"
    )
    other_template_pk = await _insert_template(
        session, tenant_id=OTHER_TENANT, template_id="tpl_exec_tenant_b"
    )
    await _insert_execution(
        session,
        tenant_id=OTHER_TENANT,
        template_pk=other_template_pk,
        version=1,
        render_status="ok",
        created_at_sql="now()",
    )

    # 本租户用户访问其它租户模板：404。
    cross = await client.get(
        f"{BASE}/{other_template_pk}/executions",
        headers=auth_headers(viewer_token),
    )
    assert cross.status_code == 404, cross.text

    # 其它租户用户访问本租户模板：404。
    reverse = await client.get(
        f"{BASE}/{our_template_pk}/executions",
        headers=auth_headers(other_tenant_token),
    )
    assert reverse.status_code == 404, reverse.text

    # 未知模板：404。
    unknown = await client.get(
        f"{BASE}/99999999/executions",
        headers=auth_headers(viewer_token),
    )
    assert unknown.status_code == 404, unknown.text


@pytest.mark.asyncio
async def test_executions_empty_page(client, viewer_token, session):
    template_pk = await _insert_template(
        session, tenant_id=TENANT, template_id="tpl_exec_empty"
    )

    response = await client.get(
        f"{BASE}/{template_pk}/executions",
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
