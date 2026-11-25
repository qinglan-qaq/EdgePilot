"""混合检索：pgvector cosine + PostgreSQL 全文 + pg_trgm，RRF 融合，可选重排。

真实分数口径：

- ``vector``：``1 - (embedding <=> query_vec)``，仅当 embedding provider 可用且
  库内 chunk 有 embedding 时存在；
- ``fts``：``ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', q))``；
- ``trigram``：``word_similarity(query, content)``；
- ``rrf``：``sum(1 / (k + rank))``，k=60；
- ``rerank``：仅当 reranker provider 可用时由 provider 返回。

任何不可用/无候选的腿都写 ``None``，**不填 0 冒充真实分数**。
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeChunk
from app.observability import get_tracer
from app.observability.records import RetrievalRecord
from app.observability.span import NullSpan
from app.rag.embedding import (
    EmbeddingProvider,
    NullEmbeddingProvider,
    ensure_dimension,
)
from app.rag.reranking import (
    NullRerankerProvider,
    RerankerProvider,
    validate_rerank_scores,
)

CANDIDATE_LIMIT = 20
DEFAULT_TOP_K = 8
RRF_K = 60
SNIPPET_LIMIT = 300

logger = logging.getLogger(__name__)


def _open_retrieval_span(tracer):
    """观测是旁路能力：span 创建失败也返回 NullSpan，绝不打断检索。"""
    if tracer is None:
        return NullSpan()
    try:
        return tracer.start_span("retrieval.hybrid", "retrieval")
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响业务
        logger.warning("retrieval span 创建失败，已跳过观测: %s", exc)
        return NullSpan()


def _obs_safe(fn, *args, **kwargs):
    """调用观测方法并吞掉异常；业务结果绝不能因埋点失败而改变。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 观测失败不影响业务
        logger.warning("retrieval 观测写入失败，已忽略: %s", exc)
        return None


@dataclass
class FusedCandidate:
    chunk_id: int
    rrf: float
    scores: dict[str, float | None] = field(default_factory=dict)


@dataclass
class _Leg:
    name: str
    rows: list[tuple[int, float]] = field(default_factory=list)


def rrf_fuse(
    legs: dict[str, list[tuple[int, float]]], *, k: int = RRF_K
) -> list[FusedCandidate]:
    """纯函数 RRF 融合。

    Args:
        legs: ``{leg_name: [(chunk_id, raw_score), ...]}``，列表顺序即排名（1 起）。
        k: RRF 常数，默认 60。

    Returns:
        按 RRF 降序（同分按 chunk_id 升序）排列的候选；每个候选保留各腿原始分。
    """
    if k <= 0:
        raise ValueError("RRF k 必须为正")

    fused: dict[int, FusedCandidate] = {}
    for leg_name, rows in legs.items():
        seen: set[int] = set()
        for rank, (chunk_id, raw_score) in enumerate(rows, start=1):
            if chunk_id in seen:  # 同一腿内重复只取最高排名
                continue
            seen.add(chunk_id)
            candidate = fused.setdefault(
                chunk_id, FusedCandidate(chunk_id=chunk_id, rrf=0.0)
            )
            candidate.rrf += 1.0 / (k + rank)
            candidate.scores[leg_name] = float(raw_score)

    return sorted(fused.values(), key=lambda item: (-item.rrf, item.chunk_id))


def _snippet(content: str) -> str:
    text = " ".join(content.split())
    if len(text) <= SNIPPET_LIMIT:
        return text
    return text[:SNIPPET_LIMIT] + "…"


@dataclass
class RetrievalOutcome:
    query: str
    results: list[dict[str, Any]]
    total_candidates: int
    empty_reason: str | None
    degraded: list[str]
    note: str
    strategy: str
    embedding_model: str | None
    reranker_model: str | None
    vector_search_unavailable: bool
    rerank_unavailable: bool
    retrieved_documents: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": self.results,
            "total_candidates": self.total_candidates,
            "empty_reason": self.empty_reason,
            "degraded": self.degraded,
            "note": self.note,
            "strategy": self.strategy,
            "embedding_model": self.embedding_model,
            "reranker_model": self.reranker_model,
            "vector_search_unavailable": self.vector_search_unavailable,
            "rerank_unavailable": self.rerank_unavailable,
        }


class HybridRetriever:
    def __init__(
        self,
        *,
        embedder: EmbeddingProvider | None = None,
        reranker: RerankerProvider | None = None,
        tracer=None,
        candidate_limit: int = CANDIDATE_LIMIT,
        rrf_k: int = RRF_K,
    ) -> None:
        self.embedder = embedder or NullEmbeddingProvider()
        self.reranker = reranker or NullRerankerProvider()
        self._tracer = tracer
        self.candidate_limit = candidate_limit
        self.rrf_k = rrf_k

    def _get_tracer(self):
        try:
            return self._tracer or get_tracer()
        except Exception as exc:  # noqa: BLE001 - 观测初始化失败不影响业务
            logger.warning("retrieval tracer 不可用，已跳过观测: %s", exc)
            return None

    # ------------------------------------------------------------------ 各腿

    async def _vector_leg(
        self, session: AsyncSession, tenant_id, query: str
    ) -> list[tuple[int, float]]:
        vectors = await self.embedder.embed([query])
        if not vectors:
            raise RuntimeError("embedding provider 返回空结果")
        ensure_dimension(vectors, dimension=self.embedder.dimension)
        vector = vectors[0]

        distance = KnowledgeChunk.embedding.cosine_distance(vector)
        stmt = (
            select(KnowledgeChunk.id, (1 - distance).label("score"))
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.embedding.isnot(None),
            )
            .order_by(distance, KnowledgeChunk.id)
            .limit(self.candidate_limit)
        )
        rows = (await session.execute(stmt)).all()
        scored: list[tuple[int, float]] = []
        for row in rows:
            if row.score is None:
                continue
            value = float(row.score)
            # 零向量等异常输入可能产生 NaN/Inf；不把非有限值当作真实分数。
            if math.isfinite(value):
                scored.append((int(row.id), value))
        return scored

    async def _fts_leg(
        self, session: AsyncSession, tenant_id, query: str
    ) -> list[tuple[int, float]]:
        tsv = func.to_tsvector("simple", KnowledgeChunk.content)
        tsq = func.plainto_tsquery("simple", query)
        rank = func.ts_rank(tsv, tsq)
        stmt = (
            select(KnowledgeChunk.id, rank.label("score"))
            .where(KnowledgeChunk.tenant_id == tenant_id, tsv.op("@@")(tsq))
            .order_by(rank.desc(), KnowledgeChunk.id)
            .limit(self.candidate_limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(int(row.id), float(row.score)) for row in rows]

    async def _trigram_leg(
        self, session: AsyncSession, tenant_id, query: str
    ) -> list[tuple[int, float]]:
        similarity = func.word_similarity(query, KnowledgeChunk.content)
        stmt = (
            select(KnowledgeChunk.id, similarity.label("score"))
            .where(KnowledgeChunk.tenant_id == tenant_id, similarity > 0)
            .order_by(similarity.desc(), KnowledgeChunk.id)
            .limit(self.candidate_limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(int(row.id), float(row.score)) for row in rows]

    # ------------------------------------------------------------------ 检索

    async def search(
        self,
        *,
        session: AsyncSession,
        tenant_id,
        query: str,
        top_k: int = DEFAULT_TOP_K,
    ) -> RetrievalOutcome:
        if not query or not query.strip():
            raise ValueError("query 不能为空")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k 必须在 1–20 之间")

        tracer = self._get_tracer()
        span = _open_retrieval_span(tracer)
        _obs_safe(span.set_input, {"query": query, "top_k": top_k})
        started = time.perf_counter()
        embedding_available = bool(getattr(self.embedder, "available", False))
        reranker_available = bool(getattr(self.reranker, "available", False))

        try:
            total_chunks = await session.scalar(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.tenant_id == tenant_id)
            )
            total_chunks = int(total_chunks or 0)

            legs: dict[str, list[tuple[int, float]]] = {}
            degraded: list[str] = []
            note_bits: list[str] = []

            if embedding_available:
                try:
                    legs["vector"] = await self._vector_leg(session, tenant_id, query)
                except Exception:  # noqa: BLE001 - 向量腿失败降级，不伪造分数
                    embedding_available = False
                    degraded.append("vector_unavailable")
                    note_bits.append("向量检索失败已跳过，未生成或伪造任何向量分数")
            else:
                degraded.append("vector_unavailable")
                note_bits.append("未配置 embedding 模型，已跳过向量腿，未伪造向量分数")

            legs["fts"] = await self._fts_leg(session, tenant_id, query)
            legs["trigram"] = await self._trigram_leg(session, tenant_id, query)

            if not reranker_available:
                degraded.append("reranker_unavailable")
                note_bits.append("未配置 reranker，按 RRF 融合排序，未伪造重排分数")

            fused = rrf_fuse(legs, k=self.rrf_k)
            chunk_ids = [candidate.chunk_id for candidate in fused]

            rows_by_id: dict[int, KnowledgeChunk] = {}
            if chunk_ids:
                rows = (
                    await session.scalars(
                        select(KnowledgeChunk).where(
                            KnowledgeChunk.tenant_id == tenant_id,
                            KnowledgeChunk.id.in_(chunk_ids),
                        )
                    )
                ).all()
                rows_by_id = {row.id: row for row in rows}

            candidates: list[dict[str, Any]] = []
            for fused_candidate in fused:
                row = rows_by_id.get(fused_candidate.chunk_id)
                if row is None:  # 理论上不会发生；绝不编造文档
                    continue
                scores = {
                    "vector": fused_candidate.scores.get("vector"),
                    "fts": fused_candidate.scores.get("fts"),
                    "trigram": fused_candidate.scores.get("trigram"),
                    "rrf": fused_candidate.rrf,
                    "rerank": None,
                }
                candidates.append(
                    {
                        "chunk_id": row.id,
                        "doc_id": row.doc_id,
                        "title": row.title,
                        "source": row.source,
                        "chunk_no": row.chunk_no,
                        "snippet": _snippet(row.content),
                        "score": fused_candidate.rrf,
                        "scores": scores,
                        "_content": row.content,
                    }
                )

            final = candidates
            strategy = "hybrid_rrf"
            if reranker_available:
                docs = [item["_content"] for item in candidates]
                try:
                    rerank_scores = validate_rerank_scores(
                        await self.reranker.rerank(query, docs), len(docs)
                    )
                    for item, rerank_score in zip(candidates, rerank_scores):
                        item["scores"]["rerank"] = rerank_score
                        item["score"] = rerank_score
                    candidates.sort(
                        key=lambda item: (
                            -float(item["scores"]["rerank"]),
                            item["chunk_id"],
                        )
                    )
                    final = candidates[:top_k]
                    strategy = "hybrid_rrf_rerank"
                except Exception:  # noqa: BLE001 - 重排失败回退 RRF，不伪造分数
                    reranker_available = False
                    if "reranker_unavailable" not in degraded:
                        degraded.append("reranker_unavailable")
                    note_bits.append("reranker 执行失败，已回退 RRF，未伪造重排分数")
                    final = candidates[:top_k]
            else:
                final = candidates[:top_k]

            results = []
            for item in final:
                clean = {key: value for key, value in item.items() if key != "_content"}
                results.append(clean)

            total_candidates = len(candidates)
            if total_chunks == 0:
                empty_reason = "knowledge_base_empty"
                note_bits.insert(0, "知识库为空：没有任何文档分块，未编造检索结果")
            elif not results:
                empty_reason = "no_match"
                note_bits.insert(0, "未命中任何知识片段；如需新增资料请先上传文档")
            else:
                empty_reason = None

            note = "；".join(note_bits) if note_bits else "混合检索完成"
            final_context = (
                "\n\n".join(
                    f"[{index}] {item['snippet']}"
                    for index, item in enumerate(results, start=1)
                )
                if results
                else None
            )

            outcome = RetrievalOutcome(
                query=query,
                results=results,
                total_candidates=total_candidates,
                empty_reason=empty_reason,
                degraded=degraded,
                note=note,
                strategy=strategy,
                embedding_model=(
                    getattr(self.embedder, "model_name", None)
                    if embedding_available
                    else None
                ),
                reranker_model=(
                    getattr(self.reranker, "model_name", None)
                    if reranker_available
                    else None
                ),
                vector_search_unavailable=not embedding_available,
                rerank_unavailable=not reranker_available,
                retrieved_documents=[
                    {key: value for key, value in item.items() if key != "_content"}
                    for item in candidates
                ],
            )
            _obs_safe(
                span.set_output,
                {
                    "total_candidates": total_candidates,
                    "result_count": len(results),
                    "empty_reason": empty_reason,
                },
            )
            _obs_safe(
                span.record_retrieval,
                RetrievalRecord(
                    query=query,
                    rewritten_query=None,
                    retrieval_strategy=strategy,
                    embedding_model=outcome.embedding_model,
                    vector_store="pgvector" if embedding_available else None,
                    reranker_model=outcome.reranker_model,
                    top_k=top_k,
                    similarity_threshold=None,
                    retrieved_documents=outcome.retrieved_documents,
                    reranked_documents=results,
                    final_context=final_context,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error=None,
                ),
            )
            _obs_safe(span.finish, "succeeded")
            return outcome
        except Exception as exc:
            error = {"code": "retrieval_error", "message": type(exc).__name__}
            _obs_safe(span.set_error, "retrieval_error", str(exc), retryable=False)
            _obs_safe(
                span.record_retrieval,
                RetrievalRecord(
                    query=query,
                    retrieval_strategy="hybrid_rrf",
                    top_k=top_k,
                    retrieved_documents=[],
                    reranked_documents=[],
                    final_context=None,
                    duration_ms=(time.perf_counter() - started) * 1000,
                    error=error,
                ),
            )
            _obs_safe(span.finish, "failed")
            raise
