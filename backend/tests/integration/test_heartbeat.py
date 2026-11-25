"""MQTT 上行 handler 与节点离线扫描的集成测试。

直接调用 handler 并显式传入租户 ID（MQTT 线程没有请求上下文）；消息去重注入
``MemoryDedupe``，不依赖 Redis / broker。所有数据写入测试事务，结束后回滚。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.messaging.mqtt.dedupe import MemoryDedupe
from app.messaging.mqtt.handlers import (
    ACKNOWLEDGED,
    CREATED,
    DUPLICATE,
    IGNORED,
    UPDATED,
    handle_event,
    handle_heartbeat,
    handle_register,
    handle_status,
    handle_task_ack,
)
from app.messaging.mqtt.sweeper import sweep_once
from app.models.device import Device, EdgeNode
from app.models.event import VisionEvent
from tests.conftest import OTHER_TENANT, TENANT


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payload(msg_id: str, **extra) -> dict:
    data = {"msg_id": msg_id, "sent_at": _now().isoformat()}
    data.update(extra)
    return data


async def _seed_node(
    session,
    node_key: str = "node-1",
    *,
    tenant_id=TENANT,
    status: str = "online",
    labels: dict | None = None,
    metrics: dict | None = None,
    last_heartbeat_at: datetime | None = None,
) -> EdgeNode:
    row = EdgeNode(
        tenant_id=tenant_id,
        node_key=node_key,
        name=node_key,
        status=status,
        labels=labels or {},
        metrics=metrics,
        last_heartbeat_at=last_heartbeat_at or _now(),
    )
    session.add(row)
    await session.flush()
    return row


async def _get_node(session, node_key: str, tenant_id=TENANT) -> EdgeNode:
    row = await session.scalar(
        select(EdgeNode).where(
            EdgeNode.tenant_id == tenant_id, EdgeNode.node_key == node_key
        )
    )
    assert row is not None
    return row


async def _count_events(session) -> int:
    rows = (await session.scalars(select(VisionEvent.id))).all()
    return len(rows)


async def test_register_is_idempotent_and_updates_version(session):
    dedupe = MemoryDedupe()

    first = await handle_register(
        session,
        TENANT,
        "node-a",
        _payload("m1", os="linux", arch="x86_64", agent_version="0.1.0", ip="10.0.0.1"),
        dedupe,
    )
    assert first == CREATED

    # 同一 msg_id 重发：不重复建节点。
    dupe = await handle_register(
        session, TENANT, "node-a", _payload("m1", agent_version="9.9.9"), dedupe
    )
    assert dupe == DUPLICATE

    # 新 msg_id：幂等 upsert 更新版本与 IP。
    updated = await handle_register(
        session,
        TENANT,
        "node-a",
        _payload("m2", agent_version="0.2.0", ip="10.0.0.2"),
        dedupe,
    )
    assert updated == UPDATED

    rows = (
        await session.scalars(select(EdgeNode).where(EdgeNode.tenant_id == TENANT))
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "node-a"
    assert row.os == "linux"
    assert row.agent_version == "0.2.0"
    assert row.ip == "10.0.0.2"
    assert row.status == "online"


async def test_register_is_scoped_by_tenant(session):
    # 同一 node_key 在两个租户下是两条独立记录。
    assert (
        await handle_register(session, TENANT, "shared", _payload("m1"), MemoryDedupe())
        == CREATED
    )
    assert (
        await handle_register(
            session, OTHER_TENANT, "shared", _payload("m2"), MemoryDedupe()
        )
        == CREATED
    )
    rows = (
        await session.scalars(select(EdgeNode).where(EdgeNode.node_key == "shared"))
    ).all()
    assert len(rows) == 2
    assert {row.tenant_id for row in rows} == {TENANT, OTHER_TENANT}


async def test_register_rejects_topic_payload_mismatch(session):
    result = await handle_register(
        session,
        TENANT,
        "node-a",
        _payload("m1", node_key="node-b"),
        MemoryDedupe(),
    )
    assert result == IGNORED
    assert await _get_node_or_none(session, "node-a") is None


async def _get_node_or_none(session, node_key: str) -> EdgeNode | None:
    return await session.scalar(
        select(EdgeNode).where(
            EdgeNode.tenant_id == TENANT, EdgeNode.node_key == node_key
        )
    )


async def test_heartbeat_updates_metrics_status_and_dedupes(session):
    await _seed_node(
        session, "node-hb", status="offline", last_heartbeat_at=_now() - timedelta(minutes=5)
    )
    dedupe = MemoryDedupe()
    before = _now()
    result = await handle_heartbeat(
        session,
        TENANT,
        "node-hb",
        _payload("h1", cpu=12.5, mem=33, gpu=None),
        dedupe,
    )
    assert result == UPDATED

    row = await _get_node(session, "node-hb")
    await session.refresh(row)
    assert row.status == "online"
    assert row.metrics == {"cpu": 12.5, "mem": 33, "gpu": None}
    assert row.last_heartbeat_at is not None
    assert row.last_heartbeat_at >= before

    # 同一 msg_id 不会重复处理；未知节点不创建假节点。
    assert (
        await handle_heartbeat(session, TENANT, "node-hb", _payload("h1"), dedupe)
        == DUPLICATE
    )
    assert (
        await handle_heartbeat(session, TENANT, "ghost", _payload("h2"), MemoryDedupe())
        == IGNORED
    )
    assert await _get_node_or_none(session, "ghost") is None


async def test_sweeper_marks_only_stale_online_nodes_offline(session):
    now = _now()
    stale = await _seed_node(
        session, "stale", status="online", last_heartbeat_at=now - timedelta(seconds=16)
    )
    fresh = await _seed_node(
        session, "fresh", status="online", last_heartbeat_at=now - timedelta(seconds=10)
    )
    disabled = await _seed_node(
        session,
        "disabled",
        status="disabled",
        labels={"disabled": True},
        last_heartbeat_at=now - timedelta(minutes=5),
    )
    already_offline = await _seed_node(
        session, "already-offline", status="offline", last_heartbeat_at=now - timedelta(minutes=5)
    )

    changed = await sweep_once(session, now=now, offline_after_s=15)
    assert changed == 1

    for row, expected in (
        (stale, "offline"),
        (fresh, "online"),
        (disabled, "disabled"),
        (already_offline, "offline"),
    ):
        await session.refresh(row)
        assert row.status == expected

    # 幂等：再扫一次没有新的超时节点。
    assert await sweep_once(session, now=now, offline_after_s=15) == 0


async def test_disabled_node_is_not_revived(session):
    await _seed_node(
        session,
        "locked",
        status="disabled",
        labels={"disabled": True, "env": "lab"},
        last_heartbeat_at=_now() - timedelta(minutes=5),
    )

    assert (
        await handle_heartbeat(session, TENANT, "locked", _payload("h1"), MemoryDedupe())
        == UPDATED
    )
    row = await _get_node(session, "locked")
    await session.refresh(row)
    assert row.status == "disabled"

    # 上线状态也不能让禁用节点复活。
    assert (
        await handle_status(session, TENANT, "locked", "online", MemoryDedupe())
        == IGNORED
    )
    await session.refresh(row)
    assert row.status == "disabled"

    assert (
        await handle_register(session, TENANT, "locked", _payload("r1"), MemoryDedupe())
        == UPDATED
    )
    await session.refresh(row)
    assert row.status == "disabled"
    assert row.labels.get("env") == "lab"


async def test_lwt_status_offline_then_online(session):
    row = await _seed_node(session, "node-status", status="online")

    # LWT 是 retained 裸字符串 "offline"，不带 msg_id。
    assert await handle_status(session, TENANT, "node-status", "offline") == UPDATED
    await session.refresh(row)
    assert row.status == "offline"

    assert (
        await handle_status(session, TENANT, "node-status", {"status": "online"})
        == UPDATED
    )
    await session.refresh(row)
    assert row.status == "online"

    assert await handle_status(session, TENANT, "node-status", "bogus") == IGNORED
    assert await handle_status(session, TENANT, "unknown-node", "online") == IGNORED


async def test_event_is_deduped_by_message(session):
    node = await _seed_node(session, "node-event")
    dedupe = MemoryDedupe()
    occurred_at = _now().isoformat()
    payload = _payload(
        "e1",
        event_type="zone",
        occurred_at=occurred_at,
        dedup_key="evt-1",
        confidence=0.91,
        bbox={"x": 1, "y": 2, "w": 3, "h": 4},
    )

    assert await handle_event(session, TENANT, "node-event", payload, dedupe) == CREATED

    # 同一 msg_id 重发，消息级去重拦截。
    assert (
        await handle_event(session, TENANT, "node-event", payload, dedupe)
        == DUPLICATE
    )

    assert await _count_events(session) == 1
    event = await session.scalar(select(VisionEvent))
    assert event is not None
    assert event.node_id == node.id
    assert event.dedup_key == "evt-1"
    assert float(event.confidence) == 0.91


async def test_event_rejects_device_from_other_node(session):
    node = await _seed_node(session, "node-a")
    other_node = await _seed_node(session, "node-b")
    device = Device(
        tenant_id=TENANT, node_id=other_node.id, name="cam-b", kind="camera",
        config={}, status="online",
    )
    session.add(device)
    await session.flush()

    result = await handle_event(
        session,
        TENANT,
        "node-a",
        _payload(
            "e-foreign",
            event_type="zone",
            occurred_at=_now().isoformat(),
            device_id=device.id,
        ),
        MemoryDedupe(),
    )
    assert result == IGNORED
    assert await _count_events(session) == 0
    assert node.id != other_node.id


async def test_task_ack_is_log_only_and_deduped(session):
    dedupe = MemoryDedupe()
    payload = _payload("a1", task_id="task-1", status="succeeded")
    assert await handle_task_ack(session, TENANT, "node-a", payload, dedupe) == ACKNOWLEDGED
    assert await handle_task_ack(session, TENANT, "node-a", payload, dedupe) == DUPLICATE
    # ack 不落业务表，只消费消息。
    assert await _count_events(session) == 0
