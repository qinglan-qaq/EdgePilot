"""可选的 reranker provider。

默认 ``NullRerankerProvider`` 不可用：检索按 RRF 排序并标记
``reranker_unavailable`` / ``reranker_model=null``，不伪造任何重排分数。

部署方可注入实现；接口只要求返回与输入等长、有限的分值列表。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class RerankerProvider(Protocol):
    available: bool
    model_name: str | None

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长的相关性分值；不可用时抛 RuntimeError。"""
        ...


@dataclass
class NullRerankerProvider:
    available: bool = False
    model_name: str | None = None

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        raise RuntimeError("reranker provider 不可用")


def get_reranker_provider(settings=None) -> RerankerProvider:
    """当前版本不内置真实 reranker，恒返回不可用 provider。"""
    return NullRerankerProvider()


def validate_rerank_scores(scores: list[float], expected: int) -> list[float]:
    if len(scores) != expected:
        raise ValueError(f"reranker 返回 {len(scores)} 条分数，期望 {expected} 条")
    result: list[float] = []
    for score in scores:
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("reranker 返回了非有限分数")
        result.append(value)
    return result
