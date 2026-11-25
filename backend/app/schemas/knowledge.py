"""知识库 API 的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=8, ge=1, le=20)


class KnowledgeDocumentOut(BaseModel):
    doc_id: str
    title: str | None = None
    source: str | None = None
    chunk_count: int
    created_at: datetime | None = None


class KnowledgeUploadOut(BaseModel):
    doc_id: str
    title: str
    source: str
    chunk_count: int
    created: bool
    duplicate: bool
    embedding_available: bool
    embedding_model: str | None = None


class KnowledgeDeleteOut(BaseModel):
    doc_id: str
    deleted_chunks: int
