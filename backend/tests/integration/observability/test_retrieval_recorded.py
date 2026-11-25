"""Retrieval 埋点：真实 Tracer/Collector/Writer 写入 retrieval_records。"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.collector import EventCollector
from app.observability.tracer import Tracer
from app.observability.writer import BatchWriter
from app.rag.retriever import HybridRetriever
from app.rag.service import RagService

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")

SEED_TEXT = "\n\n".join(
    [
        "消防通道巡检记录：通道必须保持畅通，不得堆放杂物。" * 20,
        "消防通道标识和应急照明需要定期检查。" * 20,
        "消防通道被占用时应立即通知责任人清理。" * 20,
        "消防通道巡检人员需要记录时间、位置和处理结果。" * 20,
        "消防通道整改完成后应复查并关闭工单。" * 20,
        "消防通道演练应覆盖疏散路线和集合点。" * 20,
    ]
) + "\n"


@contextmanager
def tenant(tid=TENANT):
    token = set_tenant_id(tid)
    try:
        yield tid
    finally:
        reset_tenant_id(token)


def make_tracer(writer, *, enabled: bool = True) -> Tracer:
    return Tracer(
        enabled=enabled,
        sample_rate=1.0,
        collector=EventCollector(maxsize=200, batch_size=200, flush_ms=100, writer=writer),
    )


async def _seed_chunks(session) -> str:
    result = await RagService(session).ingest_text(
        tenant_id=TENANT,
        filename="fire.md",
        text=SEED_TEXT,
        title="消防通道巡检",
        commit=False,
    )
    await session.flush()
    return result.doc_id


async def test_retrieval_span_writes_one_record(session, session_factory):
    await _seed_chunks(session)
    writer = BatchWriter(session_factory)
    tracer = make_tracer(writer)

    with tenant():
        trace = tracer.start_trace("rag-e2e")
        outcome = await HybridRetriever(tracer=tracer).search(
            session=session,
            tenant_id=TENANT,
            query="消防通道",
            top_k=8,
        )
        trace.close("succeeded")
        await writer.write(tracer.collector.pending())

    assert outcome.results
    rows = (
        await session.execute(
            text(
                "SELECT tenant_id, span_id, query, retrieved_documents,"
                " reranked_documents, final_context, reranker_model, embedding_model,"
                " vector_store, duration_ms"
                " FROM retrieval_records"
            )
        )
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == TENANT
    assert row.query == "消防通道"
    assert row.span_id and len(row.span_id) == 16

    retrieved = row.retrieved_documents
    if isinstance(retrieved, str):
        retrieved = json.loads(retrieved)
    assert len(retrieved) >= 5
    assert all(item["score"] is not None for item in retrieved)
    assert all("scores" in item for item in retrieved)

    reranked = row.reranked_documents
    if isinstance(reranked, str):
        reranked = json.loads(reranked)
    assert reranked
    assert all(item["score"] is not None for item in reranked)
    assert row.reranker_model is None
    assert row.embedding_model is None
    assert row.vector_store is None
    assert row.duration_ms is not None
    assert row.final_context

    span_row = (
        await session.execute(
            text(
                "SELECT span_id, span_type, name, status FROM spans"
                " WHERE span_type='retrieval'"
            )
        )
    ).one()
    assert span_row.span_id == row.span_id
    assert span_row.name == "retrieval.hybrid"
    assert span_row.status == "succeeded"


async def test_retrieval_without_active_trace_still_works_and_writes_nothing(
    session, session_factory
):
    await _seed_chunks(session)
    writer = BatchWriter(session_factory)
    tracer = make_tracer(writer)

    outcome = await HybridRetriever(tracer=tracer).search(
        session=session,
        tenant_id=TENANT,
        query="消防通道",
        top_k=5,
    )
    assert outcome.results
    await writer.write(tracer.collector.pending())

    count = await session.scalar(text("SELECT count(*) FROM retrieval_records"))
    assert count == 0


async def test_retrieval_with_obs_disabled_still_works(session, session_factory):
    await _seed_chunks(session)
    writer = BatchWriter(session_factory)
    tracer = make_tracer(writer, enabled=False)

    with tenant():
        trace = tracer.start_trace("rag-disabled")
        outcome = await HybridRetriever(tracer=tracer).search(
            session=session,
            tenant_id=TENANT,
            query="消防通道",
            top_k=5,
        )
        trace.close("succeeded")
        await writer.write(tracer.collector.pending())

    assert outcome.results
    count = await session.scalar(text("SELECT count(*) FROM retrieval_records"))
    assert count == 0


class ExplodingTracer:
    def start_span(self, *_args, **_kwargs):
        raise RuntimeError("tracer down")


class ExplodingSpan:
    def set_input(self, *_args, **_kwargs):
        raise RuntimeError("span input down")

    def set_output(self, *_args, **_kwargs):
        raise RuntimeError("span output down")

    def set_error(self, *_args, **_kwargs):
        raise RuntimeError("span error down")

    def record_retrieval(self, *_args, **_kwargs):
        raise RuntimeError("record down")

    def finish(self, *_args, **_kwargs):
        raise RuntimeError("finish down")


class BrokenSpanTracer:
    def start_span(self, *_args, **_kwargs):
        return ExplodingSpan()


async def test_observability_failure_never_breaks_retrieval(session):
    """span 创建或任一埋点调用异常，检索都仍返回真实结果。"""
    await _seed_chunks(session)

    no_span = await HybridRetriever(tracer=ExplodingTracer()).search(
        session=session, tenant_id=TENANT, query="消防通道", top_k=3
    )
    assert no_span.results
    assert no_span.results[0]["score"] is not None

    broken_span = await HybridRetriever(tracer=BrokenSpanTracer()).search(
        session=session, tenant_id=TENANT, query="消防通道", top_k=3
    )
    assert broken_span.results
    assert broken_span.results[0]["score"] is not None
