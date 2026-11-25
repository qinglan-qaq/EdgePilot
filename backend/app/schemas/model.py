"""模型配置的请求/响应模型。

响应模型 ``ModelOut`` 刻意不包含 ``api_key`` / ``api_key_enc``：密钥只在
写入时进入服务层，任何 API 响应都不得回显。
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# 字段名 model_type/model_name 与本项目语义一致，关闭 Pydantic 的 model_ 保护命名空间告警。
_BASE_CONFIG = ConfigDict(protected_namespaces=())


class ModelCreate(BaseModel):
    model_config = _BASE_CONFIG

    name: str
    model_type: str
    provider: str
    model_name: str
    base_url: str | None = None
    deployment_location: str | None = None
    params: dict = Field(default_factory=dict)
    api_key: str | None = None
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    cached_price_per_1k: Decimal | None = None


class ModelUpdate(BaseModel):
    model_config = _BASE_CONFIG

    name: str | None = None
    model_type: str | None = None
    provider: str | None = None
    model_name: str | None = None
    base_url: str | None = None
    deployment_location: str | None = None
    params: dict | None = None
    api_key: str | None = None
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    cached_price_per_1k: Decimal | None = None


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    name: str
    model_type: str
    provider: str
    model_name: str
    base_url: str | None
    params: dict
    deployment_location: str | None
    status: str
    input_price_per_1k: Decimal | None
    output_price_per_1k: Decimal | None
    cached_price_per_1k: Decimal | None
    created_at: datetime | None = None
    updated_at: datetime | None = None
