"""领域 Agent 试跑：诊断角色的成功落库与「缺证据」失败语义（Task 13）。"""

import pytest

from tests.conftest import FIXTURE_DIR, _make_published_agent, auth_headers


@pytest.fixture(autouse=True)
def deterministic_obs_and_llm(monkeypatch):
    """与 ``test_agent_test_run.py`` 相同：mock 模式 + 固定 fixture 目录。"""
    import app.observability as obs
    from app.core.config import get_settings

    monkeypatch.setenv("OBS_ENABLED", "true")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("LLM_FIXTURE_DIR", str(FIXTURE_DIR))
    get_settings.cache_clear()
    obs.reset_tracer()
    yield
    obs.reset_tracer()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_diagnosis_agent_succeeds_with_evidence(client, admin_token):
    agent_id = await _make_published_agent(
        client,
        admin_token,
        "diagnosis-check",
        "diagnosis_ok",
        agent_type="diagnosis",
        config_extra={"system_prompt": "你是设备诊断助手"},
    )

    r = await client.post(
        f"/api/v1/agents/{agent_id}/test",
        json={"task": {"question": "机柜温度告警原因"}},
        headers=auth_headers(admin_token),
    )
    data = r.json()["data"]

    assert data["status"] == "succeeded"
    assert data["output"]["conclusion"] == "设备过热"
    assert len(data["output"]["evidence"]) == 1
    assert data["output"]["evidence"][0]["ref"] == "m1"
    assert data["retry_count"] == 0


@pytest.mark.asyncio
async def test_diagnosis_without_evidence_fails_after_one_retry(client, admin_token):
    agent_id = await _make_published_agent(
        client,
        admin_token,
        "diagnosis-no-evidence",
        "diagnosis_no_evidence",
        agent_type="diagnosis",
        config_extra={"system_prompt": "你是设备诊断助手"},
    )

    r = await client.post(
        f"/api/v1/agents/{agent_id}/test",
        json={"task": {"question": "机柜温度告警原因"}},
        headers=auth_headers(admin_token),
    )
    data = r.json()["data"]

    assert data["status"] == "failed"
    assert "agent_output_invalid" in (data["error"] or "")
    assert data["retry_count"] == 1
