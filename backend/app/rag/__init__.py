"""平台 RAG：文档解析、标题/递归分块、可选向量化与重排、混合检索。

设计约束（Task 20）：

- 没有 embedding 模型时向量腿跳过、记为 NULL，绝不生成随机/哈希假向量；
- 没有 reranker 时按 RRF 排序，绝不伪造重排分数；
- 空知识库/无命中统一返回 ``[]`` + ``empty_reason``，不编造文档。
"""

from app.rag.chunking import Chunk, chunk_document, chunk_text
from app.rag.errors import (
    DocumentEmptyError,
    DocumentParseError,
    RagError,
    UnsupportedDocumentType,
)
from app.rag.parsing import ParsedDocument, parse_document, parse_text
from app.rag.retriever import HybridRetriever, RetrievalOutcome, rrf_fuse

__all__ = [
    "Chunk",
    "chunk_document",
    "chunk_text",
    "RagError",
    "UnsupportedDocumentType",
    "DocumentEmptyError",
    "DocumentParseError",
    "ParsedDocument",
    "parse_document",
    "parse_text",
    "HybridRetriever",
    "RetrievalOutcome",
    "rrf_fuse",
]
