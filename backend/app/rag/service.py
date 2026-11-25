"""RAG 领域服务：入库 + 混合检索。

API 层只负责 HTTP 语义；本服务负责确定性 doc_id、分块、可选向量化与事务边界。
没有 embedding provider 时 embedding 写 NULL 且在 chunk metadata 标记
``embedding_available=false``，绝不生成假向量。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeChunk
from app.rag.chunking import chunk_document
from app.rag.embedding import (
    EMBEDDING_DIMENSION,
    EmbeddingProvider,
    ensure_dimension,
    get_embedding_provider,
)
from app.rag.errors import DocumentEmptyError
from app.rag.reranking import RerankerProvider, get_reranker_provider
from app.rag.retriever import (
    DEFAULT_TOP_K,
    HybridRetriever,
    RetrievalOutcome,
)
from app.repositories.knowledge_repo import KnowledgeRepository


def compute_doc_id(tenant_id: uuid.UUID, filename: str, text: str) -> str:
    """确定性 doc_id：同租户 + 同文件名 + 同解析文本 => 同 doc_id。"""
    digest = hashlib.sha256(f"{tenant_id}:{filename}:{text}".encode("utf-8")).hexdigest()
    return digest[:32]


@dataclass
class IngestResult:
    doc_id: str
    title: str
    source: str
    chunk_count: int
    created: bool
    embedding_available: bool
    embedding_model: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "source": self.source,
            "chunk_count": self.chunk_count,
            "created": self.created,
            "duplicate": not self.created,
            "embedding_available": self.embedding_available,
            "embedding_model": self.embedding_model,
        }


class RagService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: EmbeddingProvider | None = None,
        reranker: RerankerProvider | None = None,
        tracer=None,
    ) -> None:
        self.session = session
        self.repo = KnowledgeRepository(session)
        self.embedder = embedder or get_embedding_provider()
        self.reranker = reranker or get_reranker_provider()
        self._tracer = tracer

    async def ingest_text(
        self,
        *,
        tenant_id: uuid.UUID,
        filename: str,
        text: str,
        title: str | None = None,
        commit: bool = True,
    ) -> IngestResult:
        source = filename
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            raise DocumentEmptyError()

        doc_id = compute_doc_id(tenant_id, filename, normalized)
        existing = await self.repo.count_document(tenant_id, doc_id)
        if existing:
            # 幂等：同一 doc_id 不重复写 chunk，返回库内真实数量与真实 embedding 状态。
            first = (
                await self.session.scalars(
                    select(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.tenant_id == tenant_id,
                        KnowledgeChunk.doc_id == doc_id,
                    )
                    .limit(1)
                )
            ).first()
            stored_available = bool(first is not None and first.embedding is not None)
            stored_model = (
                (first.meta or {}).get("embedding_model") if stored_available else None
            )
            return IngestResult(
                doc_id=doc_id,
                title=(first.title if first else title) or filename,
                source=(first.source if first else source) or source,
                chunk_count=existing,
                created=False,
                embedding_available=stored_available,
                embedding_model=stored_model,
            )

        chunks = chunk_document(normalized)
        if not chunks:
            raise DocumentEmptyError("文档分块结果为空，未写入知识库")

        embedding_available = bool(getattr(self.embedder, "available", False))
        embedding_model = (
            getattr(self.embedder, "model_name", None) if embedding_available else None
        )
        vectors: list[list[float] | None] = [None] * len(chunks)
        if embedding_available:
            embedded = await self.embedder.embed([chunk.content for chunk in chunks])
            ensure_dimension(embedded, dimension=EMBEDDING_DIMENSION)
            vectors = list(embedded)

        resolved_title = (title or chunks[0].content.splitlines()[0][:255]).strip()
        if not resolved_title:
            resolved_title = filename

        rows = [
            KnowledgeChunk(
                tenant_id=tenant_id,
                doc_id=doc_id,
                title=resolved_title,
                source=source,
                chunk_no=chunk.chunk_no,
                content=chunk.content,
                embedding=vectors[index],
                meta={
                    "chunk_start": chunk.start,
                    "chunk_end": chunk.end,
                    "heading": chunk.heading,
                    "embedding_available": embedding_available,
                    "embedding_model": embedding_model,
                },
            )
            for index, chunk in enumerate(chunks)
        ]

        try:
            await self.repo.add_chunks(rows)
            if commit:
                await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        return IngestResult(
            doc_id=doc_id,
            title=resolved_title,
            source=source,
            chunk_count=len(rows),
            created=True,
            embedding_available=embedding_available,
            embedding_model=embedding_model,
        )

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        query: str,
        top_k: int = DEFAULT_TOP_K,
    ) -> RetrievalOutcome:
        retriever = HybridRetriever(
            embedder=self.embedder,
            reranker=self.reranker,
            tracer=self._tracer,
        )
        return await retriever.search(
            session=self.session,
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
        )
