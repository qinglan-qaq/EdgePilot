"""批量写入：把 collector 的原始记录落到 traces/spans/events/llm_calls/tool_calls。

约定：
- 每条记录自带 tenant_id，写入端绝不读租户上下文。
- 整批共用一个事务；任何一条记录失败都向上抛，交给 collector 的重试逻辑。
- 事务提交后才推 Redis 实时层，实时层失败不影响落库。
- 子记录（llm_calls / tool_calls / retrievals / events）不带 span_id/trace_id/
  tenant_id，由所属 span.close / trace.close 记录注入。
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic_core import to_jsonable_python
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert

from app.observability.live import LiveLayer
from app.observability.models.event import TraceEvent
from app.observability.models.llm_call import LLMCall
from app.observability.models.retrieval import RetrievalRecord as RetrievalRecordModel
from app.observability.models.span import Span
from app.observability.models.tool_call import ToolCall
from app.observability.models.trace import Trace

logger = logging.getLogger(__name__)

# retrieval_records 要等 0004 迁移才建；写入前用 to_regclass 探测，缺失时跳过整批里
# 的检索记录而不是让整批失败。写入使用 ORM 表，保证 asyncpg 拿到正确类型
# （尤其是 JSONB 数组），列集合与 _RETRIEVAL_COLUMNS 一致、不含 attempt。
_RETRIEVAL_TABLE = RetrievalRecordModel.__table__

_LLM_JSON_FIELDS = (
    "tool_definitions",
    "request_messages",
    "raw_response",
    "structured_response",
    "error",
)
_TOOL_JSON_FIELDS = (
    "input_schema",
    "output_schema",
    "input_arguments",
    "output_result",
    "error",
)
_RETRIEVAL_JSON_FIELDS = (
    "retrieved_documents",
    "reranked_documents",
    "error",
)

_LLM_COLUMNS = (
    "provider",
    "model_name",
    "model_version",
    "temperature",
    "top_p",
    "max_tokens",
    "tool_definitions",
    "tool_choice",
    "request_messages",
    "raw_response",
    "structured_response",
    "response_format",
    "finish_reason",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "total_tokens",
    "cost",
    "request_id",
    "error",
)
_TOOL_COLUMNS = (
    "tool_id",
    "tool_name",
    "tool_version",
    "description",
    "input_schema",
    "output_schema",
    "input_arguments",
    "output_result",
    "execution_status",
    "permission_result",
    "duration_ms",
    "retry_count",
    "error",
)
_RETRIEVAL_COLUMNS = (
    "query",
    "rewritten_query",
    "retrieval_strategy",
    "embedding_model",
    "vector_store",
    "reranker_model",
    "top_k",
    "similarity_threshold",
    "retrieved_documents",
    "reranked_documents",
    "final_context",
    "duration_ms",
    "error",
)


def _jsonable(value: Any) -> Any:
    """datetime/UUID/Decimal/dataclass 统一转成 asyncpg 可接受的 JSON 值。"""
    if value is None:
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    try:
        return to_jsonable_python(value)
    except Exception:
        return json.loads(json.dumps(value, default=str))


def _as_dict(record: Any) -> dict:
    if dataclasses.is_dataclass(record) and not isinstance(record, type):
        return dataclasses.asdict(record)
    if isinstance(record, dict):
        return dict(record)
    return dict(vars(record))


def _end_time(start: datetime | None, duration_ms: float | None) -> datetime | None:
    if start is None or duration_ms is None:
        return None
    return start + timedelta(milliseconds=duration_ms)


def _sub_values(record: Any, *, tenant_id, span_id: str, attempt: int,
                columns: tuple[str, ...], json_fields: tuple[str, ...]) -> dict:
    data = _as_dict(record)
    values = {"tenant_id": tenant_id, "span_id": span_id, "attempt": attempt}
    for column in columns:
        value = data.get(column)
        values[column] = _jsonable(value) if column in json_fields else value
    return values


class BatchWriter:
    def __init__(self, session_factory, live: LiveLayer | None = None):
        self._session_factory = session_factory
        self._live = live

    async def write(self, records: list[dict]) -> None:
        if not records:
            return
        async with self._session_factory() as s:
            async with s.begin():
                for record in records:
                    await self._dispatch(s, record)
        if self._live is not None:
            try:
                await self._live.publish_many(records)
            except Exception:  # 实时层永不影响落库
                logger.debug("obs live publish failed", exc_info=True)

    async def _dispatch(self, s, record: dict) -> None:
        kind = record.get("kind")
        if kind == "trace.start":
            await self._trace_start(s, record)
        elif kind == "span.open":
            await self._span_open(s, record)
        elif kind == "span.close":
            await self._span_close(s, record)
        elif kind == "trace.close":
            await self._trace_close(s, record)
        else:
            logger.debug("unknown obs record kind: %r", kind)

    # ---- trace / span ---------------------------------------------------
    async def _trace_start(self, s, record: dict) -> None:
        values = {
            "tenant_id": record["tenant_id"],
            "trace_id": record["trace_id"],
            "root_span_id": record.get("root_span_id"),
            "name": record.get("name") or "",
            "source": record.get("source") or "unknown",
            "user_id": record.get("user_id"),
            "session_id": record.get("session_id"),
            "status": "running",
            "input": _jsonable(record.get("input")),
            "start_time": record["start_time"],
            "metadata": _jsonable(record.get("metadata") or {}),
        }
        stmt = (
            pg_insert(Trace.__table__)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["trace_id"])
        )
        await s.execute(stmt)

    async def _span_open(self, s, record: dict) -> None:
        values = {
            "tenant_id": record["tenant_id"],
            "trace_id": record["trace_id"],
            "span_id": record["span_id"],
            "parent_span_id": record.get("parent_span_id"),
            "name": record.get("name") or "",
            "span_type": record.get("span_type") or "workflow",
            "status": "running",
            "attempt": record.get("attempt") or 1,
            "start_time": record["start_time"],
            "input": _jsonable(record.get("input")),
            "metadata": _jsonable(record.get("metadata") or {}),
        }
        stmt = (
            pg_insert(Span.__table__)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["span_id"])
        )
        await s.execute(stmt)

    async def _span_close(self, s, record: dict) -> None:
        tenant_id = record["tenant_id"]
        span_id = record["span_id"]
        attempt = record.get("attempt") or 1
        duration_ms = record.get("duration_ms")

        values = {
            "tenant_id": tenant_id,
            "trace_id": record["trace_id"],
            "span_id": span_id,
            "parent_span_id": record.get("parent_span_id"),
            "name": record.get("name") or "",
            "span_type": record.get("span_type") or "workflow",
            "agent_id": record.get("agent_id"),
            "agent_version": record.get("agent_version"),
            "node_id": record.get("node_id"),
            "tool_id": record.get("tool_id"),
            "status": record.get("status") or "succeeded",
            "attempt": attempt,
            "start_time": record["start_time"],
            "end_time": _end_time(record.get("start_time"), duration_ms),
            "duration_ms": duration_ms,
            "input": _jsonable(record.get("input")),
            "output": _jsonable(record.get("output")),
            "error": _jsonable(record.get("error")),
            "metadata": _jsonable(record.get("metadata") or {}),
        }
        stmt = pg_insert(Span.__table__).values(**values)
        # set_ 的键必须用 Column 对象：字符串 "metadata" 会被解析成 Base.metadata
        span_columns = Span.__table__.c
        stmt = stmt.on_conflict_do_update(
            index_elements=["span_id"],
            set_={span_columns[key]: stmt.excluded[key]
                  for key in values if key != "span_id"},
        )
        await s.execute(stmt)

        for rec in record.get("llm_calls") or []:
            await s.execute(pg_insert(LLMCall.__table__).values(**_sub_values(
                rec, tenant_id=tenant_id, span_id=span_id, attempt=attempt,
                columns=_LLM_COLUMNS, json_fields=_LLM_JSON_FIELDS)))

        for rec in record.get("tool_calls") or []:
            await s.execute(pg_insert(ToolCall.__table__).values(**_sub_values(
                rec, tenant_id=tenant_id, span_id=span_id, attempt=attempt,
                columns=_TOOL_COLUMNS, json_fields=_TOOL_JSON_FIELDS)))

        await self._write_retrievals(
            s, record.get("retrievals") or [],
            tenant_id=tenant_id, span_id=span_id,
        )
        await self._write_events(
            s, record.get("events") or [],
            tenant_id=tenant_id, trace_id=record["trace_id"], span_id=span_id,
        )

    async def _trace_close(self, s, record: dict) -> None:
        end_time = _end_time(record.get("start_time"), record.get("duration_ms"))
        stmt = text("""
            UPDATE traces t SET
              status = :status,
              output = :output,
              end_time = :end_time,
              duration_ms = :duration_ms,
              total_spans = (SELECT count(*) FROM spans s WHERE s.trace_id = t.trace_id),
              error_count = (SELECT count(*) FROM spans s WHERE s.trace_id = t.trace_id
                             AND s.status IN ('failed','timeout')),
              total_tokens = COALESCE((SELECT sum(l.total_tokens) FROM llm_calls l
                             JOIN spans s ON s.span_id = l.span_id
                             WHERE s.trace_id = t.trace_id), 0),
              total_cost = (SELECT sum(l.cost) FROM llm_calls l
                             JOIN spans s ON s.span_id = l.span_id
                             WHERE s.trace_id = t.trace_id),
              cost_unavailable_count = (SELECT count(*) FROM llm_calls l
                             JOIN spans s ON s.span_id = l.span_id
                             WHERE s.trace_id = t.trace_id AND l.cost IS NULL)
            WHERE t.trace_id = :trace_id
        """).bindparams(bindparam("output", type_=JSONB))
        await s.execute(stmt, {
            "trace_id": record["trace_id"],
            "status": record.get("status") or "succeeded",
            "output": _jsonable(record.get("output")),
            "end_time": end_time,
            "duration_ms": record.get("duration_ms"),
        })

        root_span_id = record.get("root_span_id")
        if root_span_id:
            await s.execute(text("""
                UPDATE spans SET status = :status, end_time = :end_time,
                                 duration_ms = :duration_ms
                WHERE span_id = :root_span_id AND status = 'running'
            """), {
                "root_span_id": root_span_id,
                "status": record.get("status") or "succeeded",
                "end_time": end_time,
                "duration_ms": record.get("duration_ms"),
            })

    # ---- 1:N 子记录 ------------------------------------------------------
    async def _write_events(self, s, events: list[dict], *, tenant_id,
                            trace_id: str, span_id: str | None) -> None:
        if not events:
            return
        seq = await s.scalar(
            text("SELECT COALESCE(max(sequence_number), 0) FROM trace_events "
                 "WHERE trace_id = :t"), {"t": trace_id},
        )
        seq = int(seq or 0)
        rows = []
        for event in events:
            seq += 1
            rows.append({
                "tenant_id": tenant_id,
                "trace_id": trace_id,
                "span_id": span_id,
                "event_type": event.get("event_type") or "event",
                "event_data": _jsonable(event.get("event_data")),
                "sequence_number": seq,
            })
        await s.execute(sa.insert(TraceEvent), rows)

    async def _write_retrievals(self, s, retrievals: list, *, tenant_id,
                                span_id: str) -> None:
        if not retrievals:
            return
        exists = await s.scalar(text("SELECT to_regclass('public.retrieval_records')"))
        if exists is None:
            logger.warning(
                "retrieval_records 表不存在，跳过 %d 条检索记录", len(retrievals)
            )
            return
        rows = [
            _sub_values(rec, tenant_id=tenant_id, span_id=span_id, attempt=1,
                        columns=_RETRIEVAL_COLUMNS,
                        json_fields=_RETRIEVAL_JSON_FIELDS)
            for rec in retrievals
        ]
        for row in rows:
            row.pop("attempt", None)
        await s.execute(sa.insert(_RETRIEVAL_TABLE), rows)
