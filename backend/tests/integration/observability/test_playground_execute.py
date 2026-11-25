"""Playground 真实执行（execute=True）接入 LLMClient 的集成测试。

每个用例都把 ``LLM_FIXTURE_DIR`` 指向 ``tmp_path`` 下的独立目录，避免污染
仓库内 ``tests/fixtures/llm``。tracer 与 LLMClient 都在请求内读取 settings，
因此这里只改 env 即可同时影响两者。
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def playground_env(monkeypatch, tmp_path) -> Path:
    """OBS 开启 + mock LLM 的确定性环境；返回本次用例专用 fixture 目录。"""
    import app.observability as obs
    from app.core.config import get_settings

    fixture_dir = tmp_path / "llm"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBS_ENABLED", "true")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("LLM_FIXTURE_DIR", str(fixture_dir))
    # 采集模式走默认 REDACTED，确保 rendered_messages 会脱敏落库。
    monkeypatch.delenv("OBS_CAPTURE_MODE", raising=False)
    get_settings.cache_clear()
    obs.reset_tracer()
    yield fixture_dir
    obs.reset_tracer()
    get_settings.cache_clear()


async def _create_template_with_version(
    client,
    token: str,
    *,
    user_prompt_template: str = "U {{q}}",
    variables_schema: dict | None = None,
) -> tuple[dict, dict]:
    """通过 API 建模板 + 追加版本，返回 (template, version)。"""
    headers = auth_headers(token)
    template = (
        await client.post(
            "/api/v1/observability/prompts",
            headers=headers,
            json={
                "template_id": "playground_tpl",
                "template_name": "Playground 模板",
                "category": "test",
            },
        )
    ).json()["data"]
    version = (
        await client.post(
            f"/api/v1/observability/prompts/{template['id']}/versions",
            headers=headers,
            json={
                "system_prompt": "S",
                "user_prompt_template": user_prompt_template,
                "variables_schema": variables_schema
                or {
                    "type": "object",
                    "required": ["q"],
                    "properties": {"q": {"type": "string"}},
                },
            },
        )
    ).json()["data"]
    return template, version


@pytest.mark.asyncio
async def test_playground_execute_mock_records_trace_and_prompt_execution(
    client, admin_token, session, playground_env
):
    template, version = await _create_template_with_version(client, admin_token)
    headers = auth_headers(admin_token)
    url = f"/api/v1/observability/prompts/{template['id']}/playground"

    # 先用预览端点得到 prompt_hash（与 LLMClient 的 fixture hash 同源）。
    preview = (
        await client.post(
            url,
            headers=headers,
            json={"version": version["version"], "variables": {"q": "hi"}},
        )
    ).json()["data"]
    assert preview["status"] == "ok"

    fixture = playground_env / f"{preview['prompt_hash'][:16]}.json"
    fixture.write_text(
        json.dumps(
            {
                "content": "固定回答",
                "finish_reason": "stop",
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    r = await client.post(
        url,
        headers=headers,
        json={"version": version["version"], "variables": {"q": "hi"}, "execute": True},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "ok"
    assert data["response"]["content"] == "固定回答"
    assert data["response"]["usage"]["total_tokens"] == 12
    assert data["response"]["error_code"] is None
    assert len(data["trace_id"]) == 32
    assert len(data["span_id"]) == 16

    row = (
        await session.execute(
            text(
                "SELECT trace_id, template_version, render_status, rendered_messages,"
                " model_name FROM prompt_executions WHERE trace_id=:t"
            ),
            {"t": data["trace_id"]},
        )
    ).one()
    assert row.trace_id == data["trace_id"]
    assert row.template_version == version["version"]
    assert row.render_status == "ok"
    assert row.rendered_messages is not None
    assert row.model_name == "mock"


@pytest.mark.asyncio
async def test_playground_execute_missing_fixture_is_honest(
    client, admin_token, session, playground_env
):
    template, version = await _create_template_with_version(client, admin_token)
    headers = auth_headers(admin_token)

    r = await client.post(
        f"/api/v1/observability/prompts/{template['id']}/playground",
        headers=headers,
        json={"version": version["version"], "variables": {"q": "hi"}, "execute": True},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "llm_error"
    assert data["response"]["error_code"] == "mock_fixture_missing"
    assert data["response"]["content"] == ""
    assert data["response"]["usage"] is None
    assert len(data["trace_id"]) == 32

    row = (
        await session.execute(
            text(
                "SELECT render_status FROM prompt_executions WHERE trace_id=:t"
            ),
            {"t": data["trace_id"]},
        )
    ).one()
    assert row.render_status == "llm_error"


@pytest.mark.asyncio
async def test_playground_execute_viewer_forbidden(
    client, admin_token, viewer_token, playground_env
):
    template, version = await _create_template_with_version(client, admin_token)
    r = await client.post(
        f"/api/v1/observability/prompts/{template['id']}/playground",
        headers=auth_headers(viewer_token),
        json={"version": version["version"], "variables": {"q": "hi"}, "execute": True},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_playground_execute_render_schema_error_does_not_call_llm(
    client, admin_token, session, playground_env
):
    template, version = await _create_template_with_version(client, admin_token)
    headers = auth_headers(admin_token)
    before = await session.scalar(text("SELECT count(*) FROM prompt_executions"))

    r = await client.post(
        f"/api/v1/observability/prompts/{template['id']}/playground",
        headers=headers,
        # required 变量 q 缺失：渲染阶段就应阻断，不触达 LLM
        json={"version": version["version"], "variables": {}, "execute": True},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "schema_error"
    assert data["response"] is None
    assert data["trace_id"] is None
    assert data["span_id"] is None

    after = await session.scalar(text("SELECT count(*) FROM prompt_executions"))
    assert after == before


@pytest.mark.asyncio
async def test_playground_execute_unknown_model_config_returns_404(
    client, admin_token, session, playground_env
):
    template, version = await _create_template_with_version(client, admin_token)
    headers = auth_headers(admin_token)
    before = await session.scalar(text("SELECT count(*) FROM prompt_executions"))

    r = await client.post(
        f"/api/v1/observability/prompts/{template['id']}/playground",
        headers=headers,
        json={
            "version": version["version"],
            "variables": {"q": "hi"},
            "execute": True,
            "model_config_id": 999999,
        },
    )
    assert r.status_code == 404
    after = await session.scalar(text("SELECT count(*) FROM prompt_executions"))
    assert after == before
