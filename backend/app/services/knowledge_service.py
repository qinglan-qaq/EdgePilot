"""知识库 HTTP 服务：解析上传、列表/删除与混合检索。

分层：api/v1/knowledge.py → services/knowledge_service.py →
repositories/knowledge_repo.py + rag/* → models/knowledge.py。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.rag.parsing import parse_document, safe_filename
from app.rag.service import RagService
from app.repositories.knowledge_repo import KnowledgeRepository
from app.services.audit_service import AuditService


class KnowledgeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder=None,
        reranker=None,
        tracer=None,
    ) -> None:
        self.session = session
        self.repo = KnowledgeRepository(session)
        self.rag = RagService(
            session, embedder=embedder, reranker=reranker, tracer=tracer
        )

    async def upload(
        self,
        *,
        tenant_id: uuid.UUID,
        filename: str | None,
        data: bytes,
        title: str | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        safe_name = safe_filename(filename)
        parsed = parse_document(safe_name, data)
        result = await self.rag.ingest_text(
            tenant_id=tenant_id,
            filename=safe_name,
            text=parsed.text,
            title=title or parsed.title_hint,
            commit=True,
        )
        if result.created:
            # 审计旁路：不写文档正文，只留可定位的元信息。
            await AuditService.write(
                self.session,
                action="knowledge.upload",
                resource_type="knowledge_document",
                detail={
                    "doc_id": result.doc_id,
                    "filename": safe_name,
                    "chunk_count": result.chunk_count,
                    "embedding_available": result.embedding_available,
                },
                user=user,
                ip=ip,
                user_agent=user_agent,
            )
        return result.to_dict()

    async def list_documents(
        self, tenant_id: uuid.UUID, *, page: int = 1, size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        summaries, total = await self.repo.list_documents(
            tenant_id, page=page, size=size
        )
        items = [
            {
                "doc_id": summary.doc_id,
                "title": summary.title,
                "source": summary.source,
                "chunk_count": summary.chunk_count,
                "created_at": summary.created_at,
            }
            for summary in summaries
        ]
        return items, total

    async def delete(
        self,
        *,
        tenant_id: uuid.UUID,
        doc_id: str,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> dict[str, Any]:
        deleted = await self.repo.delete_document(tenant_id, doc_id)
        if deleted == 0:
            # 不存在与跨租户统一 404，避免泄露其它租户的 doc_id。
            raise NotFound("文档不存在")
        await self.session.commit()
        await AuditService.write(
            self.session,
            action="knowledge.delete",
            resource_type="knowledge_document",
            detail={"doc_id": doc_id, "deleted_chunks": deleted},
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return {"doc_id": doc_id, "deleted_chunks": deleted}

    async def search(
        self, *, tenant_id: uuid.UUID, query: str, top_k: int = 8
    ):
        return await self.rag.search(tenant_id=tenant_id, query=query, top_k=top_k)
