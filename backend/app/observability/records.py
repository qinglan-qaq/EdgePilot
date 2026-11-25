from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass
class LLMCallRecord:
    provider: str
    model_name: str
    model_version: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    tool_definitions: list[dict] | None = None
    tool_choice: str | None = None
    request_messages: list[dict] | None = None
    raw_response: dict | None = None
    structured_response: dict | None = None
    response_format: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    total_tokens: int | None = None
    cost: Decimal | None = None
    request_id: str | None = None
    error: dict | None = None


@dataclass
class ToolCallRecord:
    tool_id: str
    tool_name: str
    tool_version: str = "1.0.0"
    description: str | None = None
    input_schema: dict | None = None
    output_schema: dict | None = None
    input_arguments: dict | None = None
    output_result: Any = None
    execution_status: str = "succeeded"
    permission_result: str = "granted"   # granted | denied | approval_required
    duration_ms: float | None = None
    retry_count: int = 0
    error: dict | None = None


@dataclass
class RetrievalRecord:
    query: str
    rewritten_query: str | None = None
    retrieval_strategy: str | None = None
    embedding_model: str | None = None
    vector_store: str | None = None
    reranker_model: str | None = None
    top_k: int | None = None
    similarity_threshold: float | None = None
    retrieved_documents: list[dict] = field(default_factory=list)
    reranked_documents: list[dict] = field(default_factory=list)
    final_context: str | None = None
    duration_ms: float | None = None
    error: dict | None = None
