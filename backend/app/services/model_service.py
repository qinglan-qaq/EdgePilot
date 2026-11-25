"""模型配置业务逻辑。

所有查询都按 ``get_tenant_id()`` 过滤；``create`` 由调用方显式传入
``tenant_id``。密钥只在写入时用 Fernet 加密落库，读出路径不返回明文。
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt_key
from app.core.exceptions import Conflict, NotFound
from app.core.tenancy import get_tenant_id
from app.models.agent import Agent
from app.models.model_config import ModelConfig

# PATCH 允许更新的字段；api_key 单独处理，绝不直接赋值。
_UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "model_type",
        "provider",
        "model_name",
        "base_url",
        "deployment_location",
        "params",
        "input_price_per_1k",
        "output_price_per_1k",
        "cached_price_per_1k",
    }
)


class ModelService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        name: str,
        model_type: str,
        provider: str,
        model_name: str,
        base_url: str | None = None,
        deployment_location: str | None = None,
        params: dict | None = None,
        api_key: str | None = None,
        input_price_per_1k: Decimal | None = None,
        output_price_per_1k: Decimal | None = None,
        cached_price_per_1k: Decimal | None = None,
    ) -> ModelConfig:
        row = ModelConfig(
            tenant_id=tenant_id,
            name=name,
            model_type=model_type,
            provider=provider,
            model_name=model_name,
            base_url=base_url,
            deployment_location=deployment_location,
            params=params or {},
            api_key_enc=encrypt_key(api_key) if api_key else None,
            input_price_per_1k=input_price_per_1k,
            output_price_per_1k=output_price_per_1k,
            cached_price_per_1k=cached_price_per_1k,
        )
        self.session.add(row)
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("模型名称已存在") from None
        # 取回 server_default（status / created_at 等）并保持对象可用。
        await self.session.refresh(row)
        return row

    async def update(self, model_id: int, **fields: Any) -> ModelConfig:
        row = await self.get(model_id)

        # 密钥只在显式传入且非 None 时轮换；不传则保持原有密文。
        api_key = fields.pop("api_key", None)
        if api_key is not None:
            row.api_key_enc = encrypt_key(api_key)

        for key, value in fields.items():
            if key not in _UPDATABLE_FIELDS:
                raise ValueError(f"不支持更新字段: {key}")
            setattr(row, key, value)

        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            raise Conflict("模型名称已存在") from None
        await self.session.refresh(row)
        return row

    async def get(self, model_id: int) -> ModelConfig:
        row = await self.session.scalar(
            select(ModelConfig).where(
                ModelConfig.id == model_id,
                ModelConfig.tenant_id == get_tenant_id(),
            )
        )
        if row is None:
            raise NotFound("模型不存在")
        return row

    async def list(self, page: int = 1, page_size: int = 20) -> tuple[list[ModelConfig], int]:
        tenant_id = get_tenant_id()
        total = (
            await self.session.scalar(
                select(func.count())
                .select_from(ModelConfig)
                .where(ModelConfig.tenant_id == tenant_id)
            )
            or 0
        )
        rows = (
            await self.session.scalars(
                select(ModelConfig)
                .where(ModelConfig.tenant_id == tenant_id)
                .order_by(ModelConfig.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return list(rows), total

    async def delete(self, model_id: int) -> None:
        row = await self.get(model_id)
        referenced = await self.session.scalar(
            select(Agent.id)
            .where(
                Agent.tenant_id == get_tenant_id(),
                Agent.model_config_id == model_id,
            )
            .limit(1)
        )
        if referenced is not None:
            raise Conflict("模型被 Agent 引用")
        await self.session.delete(row)
        await self.session.commit()

    async def test_connection(self, model_id: int) -> dict:
        """连通性测试。

        未知 provider 一律返回未就绪，绝不伪造成功；真实最小调用待
        Task 10 的 LLMClient 落地后替换。
        """
        row = await self.get(model_id)

        if row.provider == "mock":
            return {"ok": True, "latency_ms": 0}
        if not row.api_key_enc:
            return {"ok": False, "latency_ms": 0, "error_code": "api_key_missing"}

        # TODO(Task 10): LLMClient 落地后，这里发起真实的最小调用（如 1 token
        # 完成请求）并据实返回延迟与错误码。
        return {"ok": False, "latency_ms": 0, "error_code": "llm_client_not_ready"}
