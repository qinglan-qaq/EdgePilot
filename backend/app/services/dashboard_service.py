"""Dashboard 汇总服务。

所有统计都在 SQL 内完成（``count`` / ``FILTER`` / ``date_trunc``），禁止把整表
拉回 Python 再聚合。语义约定：

- ``counts`` 是租户内全时期当前快照；
- ``events.window_*`` 与 ``events_trend`` 只看最近 ``hours`` 小时（按
  ``occurred_at``），窗口外的事件只计入 ``events.total``；
- 空集合返回 0 / 空数组，不补假数据。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_id
from app.models.agent import Agent
from app.models.device import Device, EdgeNode
from app.models.event import VisionEvent
from app.models.knowledge import KnowledgeChunk
from app.models.run import WorkflowRun
from app.models.workflow import Workflow, WorkflowVersion

HIGH_SEVERITIES = ("high", "critical")
RECENT_RUN_LIMIT = 10


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _counts_query(model, tenant_id, *aggregates):
    return (
        select(*aggregates)
        .select_from(model)
        .where(model.tenant_id == tenant_id)
    )


async def get_summary(session: AsyncSession, hours: int) -> dict:
    tenant_id = get_tenant_id()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=hours)

    # ---------------------------------------------------------------- counts
    agent_row = (
        await session.execute(
            _counts_query(
                Agent,
                tenant_id,
                func.count().label("total"),
                func.count().filter(Agent.status == "active").label("active"),
            )
        )
    ).one()

    workflow_row = (
        await session.execute(
            _counts_query(
                Workflow,
                tenant_id,
                func.count().label("total"),
                func.count().filter(Workflow.status == "published").label("published"),
            )
        )
    ).one()

    run_row = (
        await session.execute(
            _counts_query(
                WorkflowRun,
                tenant_id,
                func.count().label("total"),
                func.count().filter(WorkflowRun.status == "running").label("running"),
                func.count()
                .filter(WorkflowRun.status == "waiting_approval")
                .label("waiting_approval"),
                func.count()
                .filter(WorkflowRun.status == "succeeded")
                .label("succeeded"),
                func.count().filter(WorkflowRun.status == "failed").label("failed"),
            )
        )
    ).one()

    in_window = VisionEvent.occurred_at >= window_start
    high_severity = VisionEvent.severity.in_(HIGH_SEVERITIES)
    event_row = (
        await session.execute(
            _counts_query(
                VisionEvent,
                tenant_id,
                func.count().label("total"),
                func.count().filter(VisionEvent.status == "new").label("new"),
                func.count().filter(high_severity).label("high_severity"),
                func.count().filter(in_window).label("window_total"),
                func.count().filter(in_window, high_severity).label("window_high_severity"),
            )
        )
    ).one()

    node_row = (
        await session.execute(
            _counts_query(
                EdgeNode,
                tenant_id,
                func.count().label("total"),
                func.count().filter(EdgeNode.status == "online").label("online"),
            )
        )
    ).one()

    device_row = (
        await session.execute(
            _counts_query(
                Device,
                tenant_id,
                func.count().label("total"),
                func.count().filter(Device.status == "online").label("online"),
            )
        )
    ).one()

    knowledge_row = (
        await session.execute(
            _counts_query(
                KnowledgeChunk,
                tenant_id,
                func.count(func.distinct(KnowledgeChunk.doc_id)).label("documents"),
                func.count().label("chunks"),
            )
        )
    ).one()

    # ----------------------------------------------------------- events trend
    # 只返回有数据的桶；不做缺桶补零。
    bucket = func.date_trunc("hour", VisionEvent.occurred_at)
    trend_rows = (
        await session.execute(
            select(
                bucket.label("bucket"),
                func.count().label("total"),
                func.count().filter(high_severity).label("high_severity"),
            )
            .where(VisionEvent.tenant_id == tenant_id, in_window)
            .group_by(bucket)
            .order_by(bucket)
        )
    ).all()

    # ------------------------------------------------------------ recent runs
    # 没有外键也照样 join，名称以 workflow_versions + workflows 为准。
    duration_ms = func.round(
        func.extract("epoch", WorkflowRun.finished_at - WorkflowRun.started_at) * 1000
    ).cast(Integer)
    recent_rows = (
        await session.execute(
            select(
                WorkflowRun.id.label("id"),
                Workflow.name.label("workflow_name"),
                WorkflowRun.status.label("status"),
                WorkflowRun.trigger_type.label("trigger_type"),
                WorkflowRun.created_at.label("created_at"),
                duration_ms.label("duration_ms"),
                WorkflowRun.trace_id.label("trace_id"),
            )
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowVersion.tenant_id == tenant_id,
                Workflow.tenant_id == tenant_id,
            )
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .limit(RECENT_RUN_LIMIT)
        )
    ).all()

    return {
        "generated_at": _iso_utc(now),
        "window_hours": hours,
        "counts": {
            "agents": {"total": int(agent_row.total), "active": int(agent_row.active)},
            "workflows": {
                "total": int(workflow_row.total),
                "published": int(workflow_row.published),
            },
            "runs": {
                "total": int(run_row.total),
                "running": int(run_row.running),
                "waiting_approval": int(run_row.waiting_approval),
                "succeeded": int(run_row.succeeded),
                "failed": int(run_row.failed),
            },
            "events": {
                "total": int(event_row.total),
                "new": int(event_row.new),
                "high_severity": int(event_row.high_severity),
                "window_total": int(event_row.window_total),
                "window_high_severity": int(event_row.window_high_severity),
            },
            "nodes": {"total": int(node_row.total), "online": int(node_row.online)},
            "devices": {"total": int(device_row.total), "online": int(device_row.online)},
            "knowledge": {
                "documents": int(knowledge_row.documents),
                "chunks": int(knowledge_row.chunks),
            },
        },
        "events_trend": [
            {
                "bucket": _iso_utc(row.bucket),
                "total": int(row.total),
                "high_severity": int(row.high_severity),
            }
            for row in trend_rows
        ],
        "recent_runs": [
            {
                "id": row.id,
                "workflow_name": row.workflow_name,
                "status": row.status,
                "trigger_type": row.trigger_type,
                "created_at": _iso_utc(row.created_at),
                "duration_ms": (
                    int(row.duration_ms) if row.duration_ms is not None else None
                ),
                "trace_id": row.trace_id,
            }
            for row in recent_rows
        ],
    }


__all__ = ["HIGH_SEVERITIES", "RECENT_RUN_LIMIT", "get_summary"]
