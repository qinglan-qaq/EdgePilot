"""模型配置服务与路由集成测试。

这些用例需要真实数据库：密钥加密、租户过滤与唯一约束都依赖 DB 行为，
因此放在 integration 下。所有 session 绑定外层事务，测试结束统一回滚。
"""

import json

import pytest
from sqlalchemy import select

from app.core.crypto import decrypt_key
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.model_config import ModelConfig
from app.schemas.model import ModelOut
from app.services.model_service import ModelService
from tests.conftest import TENANT, auth_headers

MODEL_PAYLOAD = {
    "name": "ds",
    "model_type": "llm",
    "provider": "deepseek",
    "model_name": "deepseek-chat",
}


@pytest.fixture(autouse=True)
def tenant_ctx():
    """服务层直接调用时没有请求依赖，需要显式建立租户上下文。"""
    token = set_tenant_id(TENANT)
    try:
        yield
    finally:
        reset_tenant_id(token)


async def _get_row(session, model_id: int) -> ModelConfig:
    row = await session.scalar(select(ModelConfig).where(ModelConfig.id == model_id))
    assert row is not None
    return row


async def test_api_key_is_encrypted_at_rest(session):
    svc = ModelService(session)
    created = await svc.create(tenant_id=TENANT, api_key="sk-plain", **MODEL_PAYLOAD)

    row = await _get_row(session, created.id)
    assert row.api_key_enc != "sk-plain"
    assert decrypt_key(row.api_key_enc) == "sk-plain"
    # 密文不能原样包含明文
    assert "sk-plain" not in row.api_key_enc


async def test_model_out_never_exposes_key(session):
    svc = ModelService(session)
    created = await svc.create(tenant_id=TENANT, api_key="sk-plain", **MODEL_PAYLOAD)

    row = await _get_row(session, created.id)
    dumped = ModelOut.model_validate(row).model_dump()

    assert "api_key" not in dumped
    assert "api_key_enc" not in dumped
    serialized = json.dumps(dumped, default=str)
    assert "sk-plain" not in serialized
    assert row.api_key_enc not in serialized


async def test_prices_default_to_null(session):
    svc = ModelService(session)
    created = await svc.create(tenant_id=TENANT, **MODEL_PAYLOAD)

    row = await _get_row(session, created.id)
    assert row.input_price_per_1k is None
    assert row.output_price_per_1k is None
    assert row.cached_price_per_1k is None

    dumped = ModelOut.model_validate(row).model_dump()
    assert dumped["input_price_per_1k"] is None
    assert dumped["output_price_per_1k"] is None
    assert dumped["cached_price_per_1k"] is None


async def test_connection_mock_returns_ok(session):
    svc = ModelService(session)
    created = await svc.create(
        tenant_id=TENANT,
        name="mock-model",
        model_type="llm",
        provider="mock",
        model_name="mock",
    )

    assert await svc.test_connection(created.id) == {"ok": True, "latency_ms": 0}


async def test_connection_without_api_key_is_not_ok(session):
    svc = ModelService(session)
    created = await svc.create(tenant_id=TENANT, **MODEL_PAYLOAD)

    result = await svc.test_connection(created.id)
    assert result["ok"] is False
    assert "error_code" in result


async def test_update_can_rotate_key(session):
    svc = ModelService(session)
    created = await svc.create(tenant_id=TENANT, api_key="sk-old", **MODEL_PAYLOAD)

    row = await _get_row(session, created.id)
    old_enc = row.api_key_enc
    assert decrypt_key(old_enc) == "sk-old"

    await svc.update(created.id, api_key="sk-new")
    row = await _get_row(session, created.id)
    assert row.api_key_enc != old_enc
    assert decrypt_key(row.api_key_enc) == "sk-new"

    rotated_enc = row.api_key_enc
    await svc.update(created.id, name="ds-renamed")
    row = await _get_row(session, created.id)
    assert row.name == "ds-renamed"
    # 未传 api_key 时密文保持不变
    assert row.api_key_enc == rotated_enc
    assert decrypt_key(row.api_key_enc) == "sk-new"


async def test_models_write_requires_admin(client, admin_token, viewer_token):
    payload = {**MODEL_PAYLOAD, "api_key": "sk-plain"}

    r = await client.post(
        "/api/v1/models", json=payload, headers=auth_headers(viewer_token)
    )
    assert r.status_code == 403
    assert r.json()["code"] == 403

    r = await client.post(
        "/api/v1/models", json=payload, headers=auth_headers(admin_token)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["name"] == "ds"
    # 响应永不回显密钥
    assert "api_key" not in body["data"]
    assert "api_key_enc" not in body["data"]
    assert "sk-plain" not in r.text


async def test_models_read_allows_viewer(client, viewer_token):
    r = await client.get("/api/v1/models", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 0
