"""知识库检索内置工具。

MVP 阶段尚未接入向量化模型（embedding），这里只做 **真实表** ``knowledge_chunks``
上的关键词匹配（ILIKE）。没有命中就返回空列表，不编造片段。
"""

from sqlalchemy import or_, select

from app.models.knowledge import KnowledgeChunk
from app.tools.builtin.common import empty_payload, query_scope
from app.tools.registry import ToolContext, ToolRegistry

SEARCH_INPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["query"],
    "properties": {
        "query": {"type": "string", "minLength": 1, "maxLength": 500},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "additionalProperties": False,
}

SEARCH_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["items", "count"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "count": {"type": "integer"},
        "note": {"type": "string"},
    },
}


def _escape_like(value: str) -> str:
    """转义 ILIKE 通配符，避免用户输入被当成模式。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_knowledge_base(args: dict, ctx: ToolContext | None) -> dict:
    """在 knowledge_chunks 中按关键词检索知识片段。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note)

        pattern = f"%{_escape_like(args['query'])}%"
        stmt = (
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.tenant_id == scope.tenant_id,
                or_(
                    KnowledgeChunk.content.ilike(pattern, escape="\\"),
                    KnowledgeChunk.title.ilike(pattern, escape="\\"),
                ),
            )
            .order_by(KnowledgeChunk.doc_id, KnowledgeChunk.chunk_no)
            .limit(int(args.get("top_k") or 5))
        )
        chunks = (await scope.session.scalars(stmt)).all()
        items = [
            {
                "chunk_id": chunk.id,
                "doc_id": chunk.doc_id,
                "title": chunk.title,
                "source": chunk.source,
                "chunk_no": chunk.chunk_no,
                "snippet": chunk.content[:300] + ("…" if len(chunk.content) > 300 else ""),
                "metadata": chunk.meta or {},
            }
            for chunk in chunks
        ]
        note = "关键词匹配（ILIKE）；向量检索尚未接入，未命中返回空列表"
        return {"items": items, "count": len(items), "note": note}


def register(registry: ToolRegistry) -> None:
    registry.register(
        "search_knowledge_base",
        "1.0.0",
        SEARCH_INPUT_SCHEMA,
        SEARCH_OUTPUT_SCHEMA,
        search_knowledge_base,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="在 knowledge_chunks 中按关键词检索知识片段（向量检索暂未接入）",
    )
