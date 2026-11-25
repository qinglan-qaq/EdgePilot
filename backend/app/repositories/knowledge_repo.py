"""知识库仓储：所有查询/删除强制带 tenant_id。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeChunk


@dataclass(frozen=True)
class DocumentSummary:
    doc_id: str
    title: str | None
    source: str | None
    chunk_count: int
    created_at: datetime | None


class KnowledgeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def count_document(self, tenant_id: uuid.UUID, doc_id: str) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(
                    KnowledgeChunk.tenant_id == tenant_id,
                    KnowledgeChunk.doc_id == doc_id,
                )
            )
            or 0
        )

    async def has_chunks(self, tenant_id: uuid.UUID) -> bool:
        found = await self.session.scalar(
            select(KnowledgeChunk.id)
            .where(KnowledgeChunk.tenant_id == tenant_id)
            .limit(1)
        )
        return found is not None

    async def add_chunks(self, chunks: list[KnowledgeChunk]) -> None:
        self.session.add_all(chunks)
        await self.session.flush()

    async def list_documents(
        self, tenant_id: uuid.UUID, *, page: int = 1, size: int = 20
    ) -> tuple[list[DocumentSummary], int]:
        total = int(
            await self.session.scalar(
                select(func.count(func.distinct(KnowledgeChunk.doc_id))).where(
                    KnowledgeChunk.tenant_id == tenant_id
                )
            )
            or 0
        )

        stmt = (
            select(
                KnowledgeChunk.doc_id,
                func.max(KnowledgeChunk.title).label("title"),
                func.max(KnowledgeChunk.source).label("source"),
                func.count().label("chunk_count"),
                func.min(KnowledgeChunk.created_at).label("created_at"),
            )
            .where(KnowledgeChunk.tenant_id == tenant_id)
            .group_by(KnowledgeChunk.doc_id)
            .order_by(func.min(KnowledgeChunk.created_at).desc(), KnowledgeChunk.doc_id)
            .offset((page - 1) * size)
            .limit(size)
        )
        rows = (await self.session.execute(stmt)).all()
        summaries = [
            DocumentSummary(
                doc_id=row.doc_id,
                title=row.title,
                source=row.source,
                chunk_count=int(row.chunk_count),
                created_at=row.created_at,
            )
            for row in rows
        ]
        return summaries, total

    async def delete_document(self, tenant_id: uuid.UUID, doc_id: str) -> int:
        result = await self.session.execute(
            delete(KnowledgeChunk).where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.doc_id == doc_id,
            )
        )
        return int(result.rowcount or 0)
