"""知识库检索内置工具。

复用平台 RAG 的混合检索服务（pgvector + 全文 + pg_trgm → RRF），输出仍包含
``chunk_id/source/snippet/score``。没有命中时返回空列表 + ``note``/
``empty_reason``，绝不编造文档；没有 embedding/reranker 时在 ``degraded`` 中显式
标记，不伪造任何分数。
"""

from app.rag.service import RagService
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
        "empty_reason": {"type": ["string", "null"]},
        "degraded": {"type": "array", "items": {"type": "string"}},
        "strategy": {"type": "string"},
    },
}


async def search_knowledge_base(args: dict, ctx: ToolContext | None) -> dict:
    """在 knowledge_chunks 上执行真实混合检索。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(
                scope.note,
                empty_reason="no_context",
                degraded=[],
                strategy="unavailable",
            )

        outcome = await RagService(scope.session).search(
            tenant_id=scope.tenant_id,
            query=args["query"],
            top_k=int(args.get("top_k") or 5),
        )
        items = [
            {
                "chunk_id": item["chunk_id"],
                "doc_id": item["doc_id"],
                "title": item["title"],
                "source": item["source"],
                "chunk_no": item["chunk_no"],
                "snippet": item["snippet"],
                "score": item["score"],
                "scores": item["scores"],
            }
            for item in outcome.results
        ]
        note = outcome.note
        if outcome.empty_reason:
            note = f"{note}（empty_reason={outcome.empty_reason}）"
        return {
            "items": items,
            "count": len(items),
            "note": note,
            "empty_reason": outcome.empty_reason,
            "degraded": outcome.degraded,
            "strategy": outcome.strategy,
        }


def register(registry: ToolRegistry) -> None:
    registry.register(
        "search_knowledge_base",
        "1.1.0",
        SEARCH_INPUT_SCHEMA,
        SEARCH_OUTPUT_SCHEMA,
        search_knowledge_base,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="在知识库中执行混合检索（向量/全文/trigram + RRF），未命中返回空列表",
    )
