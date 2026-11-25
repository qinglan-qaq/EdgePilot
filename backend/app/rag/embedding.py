"""可选的 embedding provider。

生产默认使用 ``NullEmbeddingProvider``：``available=False``、模型名为 None，
入库时 embedding 列写 NULL，检索时跳过向量腿。**绝不生成随机/哈希假向量**。
需要真实向量时由部署方注入实现（例如调用本地 embedding 服务）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

EMBEDDING_DIMENSION = 1024


@runtime_checkable
class EmbeddingProvider(Protocol):
    available: bool
    model_name: str | None
    dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与输入等长的向量列表；不可用时抛 RuntimeError。"""
        ...


@dataclass
class NullEmbeddingProvider:
    """默认实现：永远不可用，供业务显式处理降级。"""

    available: bool = False
    model_name: str | None = None
    dimension: int = EMBEDDING_DIMENSION

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding provider 不可用")


def get_embedding_provider(settings=None) -> EmbeddingProvider:
    """从配置解析 provider。

    当前版本没有内置真实模型（本机未安装 sentence-transformers/torch，也不下载
    大模型），因此恒返回不可用 provider；保留函数作为未来接线的唯一入口。
    """
    return NullEmbeddingProvider()


def ensure_dimension(vectors: list[list[float]], *, dimension: int = EMBEDDING_DIMENSION) -> None:
    for index, vector in enumerate(vectors):
        if len(vector) != dimension:
            raise ValueError(
                f"embedding 维度不匹配（第 {index} 条为 {len(vector)}，期望 {dimension}）"
            )
