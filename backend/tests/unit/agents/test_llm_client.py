import asyncio
from decimal import Decimal

import pytest

from app.agents.llm import LLMClient, ProviderRawResponse


@pytest.mark.asyncio
async def test_mock_mode_hits_fixture_by_prompt_hash(tmp_path, monkeypatch):
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "abc123.json").write_text('{"content": "固定回答", "finish_reason": "stop"}',
                                         encoding="utf-8")
    client = LLMClient(mode="mock", fixture_dir=str(fixture), prompt_hash_fn=lambda m: "abc123")
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "固定回答"


@pytest.mark.asyncio
async def test_mock_mode_prefers_named_fixture(tmp_path):
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "device_ok.json").write_text(
        '{"content": "ok", "finish_reason": "stop", "usage": {"total_tokens": 20}}',
        encoding="utf-8")
    client = LLMClient(mode="mock", fixture_dir=str(fixture), fixture_name="device_ok")
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert resp.usage["total_tokens"] == 20


@pytest.mark.asyncio
async def test_mock_miss_returns_structured_refusal_not_fabrication(tmp_path):
    client = LLMClient(mode="mock", fixture_dir=str(tmp_path / "empty"), prompt_hash_fn=lambda m: "zzz")
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == ""
    assert resp.error_code == "mock_fixture_missing"


@pytest.mark.asyncio
async def test_auto_degrades_after_two_timeouts():
    client = LLMClient(mode="auto", api_key="", timeout_s=0.01)
    assert client.mode == "auto"
    for _ in range(2):
        await client.chat([{"role": "user", "content": "x"}])
    assert client.effective_mode == "mock"


@pytest.mark.asyncio
async def test_cost_is_none_when_price_missing():
    client = LLMClient(mode="mock", fixture_dir="tests/fixtures/llm", prompt_hash_fn=lambda m: "abc123")
    resp = await client.chat([{"role": "user", "content": "hi"}], model_config=None)
    assert resp.cost is None


# ---------------------------------------------------------------- 补充覆盖

@pytest.mark.asyncio
async def test_auto_switches_to_mock_after_two_consecutive_timeouts(monkeypatch, tmp_path):
    """有 key 的 auto 连续两次超时才降级；之后不再尝试 real。"""
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "abc123.json").write_text(
        '{"content": "固定回答", "finish_reason": "stop"}', encoding="utf-8")
    client = LLMClient(
        mode="auto",
        api_key="test-key-not-real",
        fixture_dir=str(fixture),
        prompt_hash_fn=lambda m: "abc123",
        timeout_s=0.01,
    )
    calls = {"n": 0}

    async def fake_call_model(**kwargs):
        calls["n"] += 1
        raise asyncio.TimeoutError

    monkeypatch.setattr(client, "_call_model", fake_call_model)

    first = await client.chat([{"role": "user", "content": "x"}])
    assert first.error_code == "timeout"
    assert first.degraded is False
    assert client.effective_mode == "real"

    second = await client.chat([{"role": "user", "content": "x"}])
    assert second.error_code == "timeout"
    assert second.degraded is True
    assert client.effective_mode == "mock"

    third = await client.chat([{"role": "user", "content": "x"}])
    assert third.content == "固定回答"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_auto_success_resets_timeout_streak(monkeypatch, tmp_path):
    """一次成功会清空连续超时计数，降级只在连续两次超时后发生。"""
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "abc123.json").write_text(
        '{"content": "固定回答", "finish_reason": "stop"}', encoding="utf-8")
    client = LLMClient(
        mode="auto",
        api_key="test-key-not-real",
        fixture_dir=str(fixture),
        prompt_hash_fn=lambda m: "abc123",
        timeout_s=0.01,
    )
    outcomes = iter(["timeout", "ok", "timeout", "timeout"])

    async def fake_call_model(**kwargs):
        if next(outcomes) == "timeout":
            raise asyncio.TimeoutError
        return ProviderRawResponse(
            content="real-ok", finish_reason="stop", usage=None,
            request_id=None, raw=None,
        )

    monkeypatch.setattr(client, "_call_model", fake_call_model)
    await client.chat([{"role": "user", "content": "x"}])
    await client.chat([{"role": "user", "content": "x"}])
    await client.chat([{"role": "user", "content": "x"}])
    # 超时-成功-超时：计数已重置，仍是 real
    assert client.effective_mode == "real"
    await client.chat([{"role": "user", "content": "x"}])
    assert client.effective_mode == "mock"


@pytest.mark.asyncio
async def test_cost_computed_from_model_config_prices(tmp_path):
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "abc123.json").write_text(
        '{"content": "ok", "finish_reason": "stop", '
        '"usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}',
        encoding="utf-8")
    client = LLMClient(mode="mock", fixture_dir=str(fixture), prompt_hash_fn=lambda m: "abc123")
    resp = await client.chat(
        [{"role": "user", "content": "hi"}],
        model_config={
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "input_price_per_1k": Decimal("0.001"),
            "output_price_per_1k": Decimal("0.002"),
        },
    )
    # 12/1000*0.001 + 8/1000*0.002 = 0.000012 + 0.000016
    assert resp.cost == Decimal("0.000028")


@pytest.mark.asyncio
async def test_real_mode_without_key_is_structured_error_not_crash():
    client = LLMClient(mode="real", api_key="", timeout_s=0.01)
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert client.effective_mode == "real"
    assert resp.error_code == "missing_api_key"
    assert resp.content == ""


@pytest.mark.asyncio
async def test_auto_uses_key_encrypted_on_model_config(monkeypatch):
    """Task 11/12 直接传 ModelConfig 行时，auto 应认出 api_key_enc 并走 real。"""
    from app.core.crypto import encrypt_key

    client = LLMClient(mode="auto", api_key="", timeout_s=0.01)
    seen: dict = {}

    async def fake_call_model(**kwargs):
        seen.update(kwargs)
        return ProviderRawResponse(content="real-ok", finish_reason="stop")

    monkeypatch.setattr(client, "_call_model", fake_call_model)
    model_config = {
        "provider": "deepseek",
        "model_name": "deepseek-chat",
        "api_key_enc": encrypt_key("sk-from-row"),
    }
    resp = await client.chat([{"role": "user", "content": "hi"}], model_config=model_config)
    assert resp.content == "real-ok"
    assert resp.provider == "deepseek"
    assert seen["api_key"] == "sk-from-row"
    assert client.effective_mode == "real"
