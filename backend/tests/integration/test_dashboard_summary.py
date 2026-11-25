"""Dashboard 汇总 API 集成测试（Task 26）。

所有统计都应与直接 SQL 聚合一致；这里用真实数据库会话插入同租户与另一租户
数据，不 mock SQL。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.device import Device, EdgeNode
from app.models.event import VisionEvent
from app.models.knowledge import KnowledgeChunk
from app.models.run import WorkflowRun
from app.models.workflow import Workflow, WorkflowVersion
from tests.conftest import OTHER_TENANT, TENANT, auth_headers

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SUMMARY_URL = "/api/v1/dashboard/summary"


async def _seed_workflow(
    session, tenant_id, name: str, *, status: str = "published"
) -> tuple[Workflow, WorkflowVersion]:
    workflow = Workflow(
        tenant_id=tenant_id,
        name=name,
        status=status,
        current_version=1 if status == "published" else 0,
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        version=1,
        dsl={"nodes": [], "edges": []},
        dsl_hash=hashlib.sha256(name.encode("utf-8")).hexdigest(),
    )
    session.add(version)
    await session.flush()
    return workflow, version


async def _seed_event(
    session,
    tenant_id,
    dedup_key: str,
    severity: str,
    occurred_at: datetime,
    *,
    status: str = "new",
) -> VisionEvent:
    row = VisionEvent(
        tenant_id=tenant_id,
        node_id=None,
        device_id=None,
        event_type="zone",
        severity=severity,
        occurred_at=occurred_at,
        dedup_key=f"{dedup_key}-{tenant_id}",
        status=status,
    )
    session.add(row)
    return row


async def _seed_full_tenant(session, tenant_id, tag: str) -> None:
    """两个 Agent、两套工作流、五种运行状态、事件/节点/设备/知识各一组。"""
    session.add_all(
        [
            Agent(
                tenant_id=tenant_id,
                name=f"agent-{tag}-active",
                agent_type="device",
                status="active",
            ),
            Agent(
                tenant_id=tenant_id,
                name=f"agent-{tag}-disabled",
                agent_type="vision",
                status="disabled",
            ),
        ]
    )

    _, published_version = await _seed_workflow(
        session, tenant_id, f"wf-{tag}-published", status="published"
    )
    await _seed_workflow(session, tenant_id, f"wf-{tag}-draft", status="draft")

    for index, status in enumerate(
        ["running", "waiting_approval", "succeeded", "failed", "pending"]
    ):
        session.add(
            WorkflowRun(
                tenant_id=tenant_id,
                workflow_version_id=published_version.id,
                trigger_type="manual",
                status=status,
                created_at=NOW - timedelta(minutes=index),
            )
        )

    for key, severity, occurred_at, status in [
        ("in-new-high", "high", NOW - timedelta(hours=1), "new"),
        ("in-new-critical", "critical", NOW - timedelta(hours=2), "new"),
        ("in-acked-high", "high", NOW - timedelta(hours=3), "acked"),
        ("in-low", "low", NOW - timedelta(hours=4), "new"),
        ("out-high", "high", NOW - timedelta(hours=30), "new"),
    ]:
        await _seed_event(session, tenant_id, key, severity, occurred_at, status=status)

    online_node = EdgeNode(
        tenant_id=tenant_id,
        node_key=f"node-{tag}-online",
        name=f"node-{tag}-online",
        status="online",
    )
    offline_node = EdgeNode(
        tenant_id=tenant_id,
        node_key=f"node-{tag}-offline",
        name=f"node-{tag}-offline",
        status="offline",
    )
    session.add_all([online_node, offline_node])
    await session.flush()

    session.add_all(
        [
            Device(
                tenant_id=tenant_id,
                node_id=online_node.id,
                name=f"device-{tag}-online",
                status="online",
            ),
            Device(
                tenant_id=tenant_id,
                node_id=offline_node.id,
                name=f"device-{tag}-offline",
                status="offline",
            ),
        ]
    )

    # knowledge_chunks 的 (doc_id, chunk_no) 唯一约束不区分租户，doc_id 必须带 tag。
    session.add_all(
        [
            KnowledgeChunk(
                tenant_id=tenant_id,
                doc_id=f"doc-{tag}-a",
                chunk_no=1,
                content="第一块",
            ),
            KnowledgeChunk(
                tenant_id=tenant_id,
                doc_id=f"doc-{tag}-a",
                chunk_no=2,
                content="第二块",
            ),
            KnowledgeChunk(
                tenant_id=tenant_id,
                doc_id=f"doc-{tag}-b",
                chunk_no=1,
                content="另一份文档",
            ),
        ]
    )
    await session.flush()


async def _count(session, model, tenant_id, *where) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.tenant_id == tenant_id, *where)
        )
        or 0
    )


async def _count_documents(session, tenant_id) -> int:
    return int(
        await session.scalar(
            select(func.count(func.distinct(KnowledgeChunk.doc_id))).where(
                KnowledgeChunk.tenant_id == tenant_id
            )
        )
        or 0
    )


async def test_dashboard_counts_match_direct_sql(
    client, session, seeded_users, admin_token
):
    await _seed_full_tenant(session, TENANT, "mine")
    await _seed_full_tenant(session, OTHER_TENANT, "other")

    response = await client.get(SUMMARY_URL, headers=auth_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    counts = data["counts"]

    assert counts["agents"]["total"] == await _count(session, Agent, TENANT)
    assert counts["agents"]["active"] == await _count(
        session, Agent, TENANT, Agent.status == "active"
    )
    assert counts["workflows"]["total"] == await _count(session, Workflow, TENANT)
    assert counts["workflows"]["published"] == await _count(
        session, Workflow, TENANT, Workflow.status == "published"
    )
    assert counts["runs"]["total"] == await _count(session, WorkflowRun, TENANT)
    for status in ("running", "waiting_approval", "succeeded", "failed"):
        assert counts["runs"][status] == await _count(
            session, WorkflowRun, TENANT, WorkflowRun.status == status
        )
    assert counts["events"]["total"] == await _count(session, VisionEvent, TENANT)
    assert counts["events"]["new"] == await _count(
        session, VisionEvent, TENANT, VisionEvent.status == "new"
    )
    assert counts["events"]["high_severity"] == await _count(
        session,
        VisionEvent,
        TENANT,
        VisionEvent.severity.in_(("high", "critical")),
    )
    assert counts["nodes"]["total"] == await _count(session, EdgeNode, TENANT)
    assert counts["nodes"]["online"] == await _count(
        session, EdgeNode, TENANT, EdgeNode.status == "online"
    )
    assert counts["devices"]["total"] == await _count(session, Device, TENANT)
    assert counts["devices"]["online"] == await _count(
        session, Device, TENANT, Device.status == "online"
    )
    assert counts["knowledge"]["documents"] == await _count_documents(session, TENANT)
    assert counts["knowledge"]["chunks"] == await _count(session, KnowledgeChunk, TENANT)

    # 另一租户确实有数据，但没有被计入本租户。
    assert await _count(session, Agent, OTHER_TENANT) == 2
    assert await _count(session, WorkflowRun, OTHER_TENANT) == 5
    assert counts["agents"]["total"] == 2
    assert counts["runs"]["total"] == 5
    assert data["window_hours"] == 24


async def test_dashboard_event_window_matches_trend(
    client, session, seeded_users, admin_token
):
    for key, severity, occurred_at, status in [
        ("in-high", "high", NOW - timedelta(hours=1), "new"),
        ("in-low", "low", NOW - timedelta(hours=2), "new"),
        ("in-critical", "critical", NOW - timedelta(hours=3), "new"),
        ("out-25h", "high", NOW - timedelta(hours=25), "new"),
        ("out-48h", "high", NOW - timedelta(hours=48), "new"),
    ]:
        await _seed_event(session, TENANT, key, severity, occurred_at, status=status)
    await _seed_event(
        session, OTHER_TENANT, "other-in", "high", NOW - timedelta(hours=1)
    )
    await session.flush()

    data = (
        await client.get(f"{SUMMARY_URL}?hours=24", headers=auth_headers(admin_token))
    ).json()["data"]
    events = data["counts"]["events"]
    trend = data["events_trend"]

    assert events["total"] == 5
    assert events["window_total"] == 3
    assert events["window_high_severity"] == 2
    assert len(trend) == 3  # 只返回有数据的桶
    assert sum(item["total"] for item in trend) == events["window_total"]
    assert sum(item["high_severity"] for item in trend) == events[
        "window_high_severity"
    ]

    buckets = [datetime.fromisoformat(item["bucket"]) for item in trend]
    assert buckets == sorted(buckets)
    assert all(bucket.tzinfo is not None for bucket in buckets)


async def test_dashboard_recent_runs_order_duration_and_isolation(
    client, session, seeded_users, admin_token
):
    _, mine_version = await _seed_workflow(session, TENANT, "巡检流程")
    _, other_version = await _seed_workflow(
        session, OTHER_TENANT, "其它租户流程"
    )

    runs: list[WorkflowRun] = []
    for index in range(12):
        created_at = NOW - timedelta(minutes=11 - index)
        # 最新的一条没有起止时间，用于验证 duration_ms 为 null。
        started_at = None if index == 11 else created_at
        finished_at = (
            None
            if index == 11
            else created_at + timedelta(seconds=1, milliseconds=500)
        )
        run = WorkflowRun(
            tenant_id=TENANT,
            workflow_version_id=mine_version.id,
            trigger_type="schedule" if index % 2 else "manual",
            status="succeeded",
            created_at=created_at,
            started_at=started_at,
            finished_at=finished_at,
            trace_id=f"trace-{index:02d}",
        )
        session.add(run)
        runs.append(run)
    other_run = WorkflowRun(
        tenant_id=OTHER_TENANT,
        workflow_version_id=other_version.id,
        trigger_type="manual",
        status="succeeded",
        created_at=NOW + timedelta(hours=1),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        trace_id="other-trace",
    )
    session.add(other_run)
    await session.flush()

    data = (
        await client.get(SUMMARY_URL, headers=auth_headers(admin_token))
    ).json()["data"]
    recent = data["recent_runs"]

    assert len(recent) == 10
    expected_ids = [run.id for run in runs[2:]][::-1]
    assert [item["id"] for item in recent] == expected_ids
    assert all(item["workflow_name"] == "巡检流程" for item in recent)
    assert other_run.id not in {item["id"] for item in recent}
    assert "other-trace" not in {item["trace_id"] for item in recent}

    assert recent[0]["trace_id"] == "trace-11"
    assert recent[0]["duration_ms"] is None
    assert all(item["duration_ms"] == 1500 for item in recent[1:])
    created_at_values = [
        datetime.fromisoformat(item["created_at"]) for item in recent
    ]
    assert created_at_values == sorted(created_at_values, reverse=True)


async def test_dashboard_empty_tenant_returns_zeros(
    client, seeded_users, other_tenant_token
):
    data = (
        await client.get(SUMMARY_URL, headers=auth_headers(other_tenant_token))
    ).json()["data"]

    assert data["counts"]["agents"] == {"total": 0, "active": 0}
    assert data["counts"]["workflows"] == {"total": 0, "published": 0}
    assert data["counts"]["runs"]["total"] == 0
    assert data["counts"]["events"]["total"] == 0
    assert data["counts"]["events"]["window_total"] == 0
    assert data["counts"]["nodes"] == {"total": 0, "online": 0}
    assert data["counts"]["devices"] == {"total": 0, "online": 0}
    assert data["counts"]["knowledge"] == {"documents": 0, "chunks": 0}
    assert data["events_trend"] == []
    assert data["recent_runs"] == []


async def test_dashboard_access_and_hours_validation(
    client, seeded_users, admin_token, viewer_token
):
    viewer = await client.get(SUMMARY_URL, headers=auth_headers(viewer_token))
    assert viewer.status_code == 200
    assert viewer.json()["code"] == 0

    anonymous = await client.get(SUMMARY_URL)
    assert anonymous.status_code == 401

    for hours in (0, 169):
        response = await client.get(
            f"{SUMMARY_URL}?hours={hours}", headers=auth_headers(admin_token)
        )
        assert response.status_code == 422, hours

    bounded = await client.get(
        f"{SUMMARY_URL}?hours=1", headers=auth_headers(admin_token)
    )
    assert bounded.status_code == 200
    assert bounded.json()["data"]["window_hours"] == 1
