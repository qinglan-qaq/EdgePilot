"""Trace 查询服务：列表筛选、详情、执行树与 span 关联数据。"""

from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound
from app.core.pagination import PageParams, PageResult
from app.core.tenancy import get_tenant_id
from app.observability.models import Span, Trace

# key 为响应中的输出键，value 为实际表名。retrieval_records 要等 0004 迁移；
# 在此之前 span 详情对该组返回空列表，不能报错。
_RELATED_TABLES = {
    "llm_calls": "llm_calls",
    "tool_calls": "tool_calls",
    "retrieval_records": "retrieval_records",
}


def build_tree(spans: list[dict]) -> list[dict]:
    nodes = {s["span_id"]: {**s, "children": [], "orphan": False} for s in spans}
    roots: list[dict] = []
    for node in nodes.values():
        parent_id = node.get("parent_span_id")
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            node["orphan"] = bool(parent_id)
            roots.append(node)

    def key(n):
        return (str(n.get("start_time") or ""), n["span_id"])

    for node in nodes.values():
        node["children"].sort(key=key)
    roots.sort(key=key)
    return roots


def _trace_summary(row: Trace) -> dict:
    return {
        "trace_id": row.trace_id,
        "name": row.name,
        "source": row.source,
        "status": row.status,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "duration_ms": row.duration_ms,
        "total_spans": row.total_spans,
        "error_count": row.error_count,
        "total_tokens": row.total_tokens,
        "total_cost": row.total_cost,
    }


def _trace_detail(row: Trace) -> dict:
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "name": row.name,
        "source": row.source,
        "user_id": row.user_id,
        "session_id": row.session_id,
        "root_span_id": row.root_span_id,
        "status": row.status,
        "input": row.input,
        "output": row.output,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "duration_ms": row.duration_ms,
        "total_tokens": row.total_tokens,
        "total_cost": row.total_cost,
        "cost_unavailable_count": row.cost_unavailable_count,
        "total_spans": row.total_spans,
        "error_count": row.error_count,
        "meta": row.meta,
        "created_at": row.created_at,
    }


def _span_dict(row: Span) -> dict:
    return {
        "id": row.id,
        "span_id": row.span_id,
        "trace_id": row.trace_id,
        "parent_span_id": row.parent_span_id,
        "name": row.name,
        "span_type": row.span_type,
        "agent_id": row.agent_id,
        "agent_version": row.agent_version,
        "workflow_id": row.workflow_id,
        "node_id": row.node_id,
        "tool_id": row.tool_id,
        "status": row.status,
        "attempt": row.attempt,
        "start_time": row.start_time,
        "end_time": row.end_time,
        "duration_ms": row.duration_ms,
        "input": row.input,
        "output": row.output,
        "error": row.error,
        "retry_count": row.retry_count,
        "meta": row.meta,
        "links": row.links,
        "created_at": row.created_at,
    }


async def list_traces(
    session: AsyncSession,
    params: PageParams,
    *,
    status: str | None = None,
    trace_id_prefix: str | None = None,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    min_duration_ms: float | None = None,
) -> PageResult[dict]:
    filters = [Trace.tenant_id == get_tenant_id()]
    if status is not None:
        filters.append(Trace.status == status)
    if trace_id_prefix:
        filters.append(Trace.trace_id.like(f"{trace_id_prefix}%"))
    if start_from is not None:
        filters.append(Trace.start_time >= start_from)
    if start_to is not None:
        filters.append(Trace.start_time <= start_to)
    if min_duration_ms is not None:
        filters.append(Trace.duration_ms >= min_duration_ms)

    total = await session.scalar(
        select(func.count()).select_from(Trace).where(*filters)
    )
    rows = (
        await session.scalars(
            select(Trace)
            .where(*filters)
            .order_by(Trace.start_time.desc(), Trace.id.desc())
            .offset(params.offset)
            .limit(params.size)
        )
    ).all()

    return PageResult(
        items=[_trace_summary(row) for row in rows],
        total=total or 0,
        page=params.page,
        size=params.size,
    )


async def get_trace(session: AsyncSession, trace_id: str) -> dict:
    row = await session.scalar(
        select(Trace).where(
            Trace.tenant_id == get_tenant_id(), Trace.trace_id == trace_id
        )
    )
    if row is None:
        raise NotFound("Trace 不存在")
    return _trace_detail(row)


async def get_tree(session: AsyncSession, trace_id: str) -> list[dict]:
    await get_trace(session, trace_id)
    rows = (
        await session.scalars(
            select(Span)
            .where(
                Span.tenant_id == get_tenant_id(),
                Span.trace_id == trace_id,
            )
            .order_by(Span.start_time, Span.id)
        )
    ).all()
    return build_tree([_span_dict(row) for row in rows])


async def _related_records(session: AsyncSession, span_id: str) -> dict[str, list[dict]]:
    """查询 span 关联的 LLM/工具/检索记录。

    `retrieval_records` 在 0004 之前不存在。先用 information_schema 探测表与必需列，
    只对确实可用（且有 tenant_id/span_id）的表发查询，避免失败查询回滚
    测试夹具尚未提交的数据。结果键与表名一致，缺失表的组返回空列表。
    """
    result: dict[str, list[dict]] = {key: [] for key in _RELATED_TABLES}
    rows = await session.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name = ANY(:names)"
        ),
        {"names": list(_RELATED_TABLES.values())},
    )
    columns: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        columns.setdefault(table_name, set()).add(column_name)

    for key, table_name in _RELATED_TABLES.items():
        available = columns.get(table_name)
        if not available or not {"tenant_id", "span_id"} <= available:
            continue
        order = " ORDER BY id" if "id" in available else ""
        records = await session.execute(
            text(
                f"SELECT * FROM {table_name}"  # 表名来自白名单，非用户输入
                f" WHERE tenant_id = :tenant_id AND span_id = :span_id{order}"
            ),
            {"tenant_id": get_tenant_id(), "span_id": span_id},
        )
        result[key] = [dict(record._mapping) for record in records]

    for record in result["llm_calls"]:
        # cost 为 NULL 表示价格缺失：只标记不可用，绝不估算。
        record["cost_available"] = record.get("cost") is not None
    return result


async def _get_span_row(session: AsyncSession, span_id: str) -> Span:
    row = await session.scalar(
        select(Span).where(
            Span.tenant_id == get_tenant_id(), Span.span_id == span_id
        )
    )
    if row is None:
        raise NotFound("Span 不存在")
    return row


async def get_span(session: AsyncSession, span_id: str) -> dict:
    row = await _get_span_row(session, span_id)
    detail = _span_dict(row)
    detail.update(await _related_records(session, span_id))
    return detail


async def get_span_children(session: AsyncSession, span_id: str) -> list[dict]:
    await _get_span_row(session, span_id)
    rows = (
        await session.scalars(
            select(Span)
            .where(
                Span.tenant_id == get_tenant_id(),
                Span.parent_span_id == span_id,
            )
            .order_by(Span.start_time, Span.id)
        )
    ).all()
    return [_span_dict(row) for row in rows]
