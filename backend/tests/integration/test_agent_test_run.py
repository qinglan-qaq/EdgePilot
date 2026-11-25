import pytest

from tests.conftest import FIXTURE_DIR, auth_headers


@pytest.fixture(autouse=True)
def deterministic_obs_and_llm(monkeypatch):
    """每次请求都重新读设置，所以这里改 env 足以同时影响 tracer 与 LLMClient。"""
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
async def test_test_run_links_agent_run_to_trace(client, admin_token, seeded_agent):
    r = await client.post(f"/api/v1/agents/{seeded_agent}/test",
                          json={"task": {"question": "设备在线吗"}},
                          headers=auth_headers(admin_token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["agent_id"] == seeded_agent
    assert data["status"] == "succeeded"
    assert len(data["trace_id"]) == 32
    assert len(data["span_id"]) == 16
    assert data["total_tokens"] == 20


@pytest.mark.asyncio
async def test_schema_invalid_output_marks_failed_after_one_retry(
        client, admin_token, seeded_agent_bad_output):
    r = await client.post(f"/api/v1/agents/{seeded_agent_bad_output}/test",
                          json={"task": {"question": "x"}},
                          headers=auth_headers(admin_token))
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert "agent_output_invalid" in (data["error"] or "")
    # 只重试 1 次：2 条 llm_calls 由观测层记录（P2 完成后断言），此处只保证失败语义
