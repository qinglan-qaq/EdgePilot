"""节点 / 设备 API 与下行指令的集成测试。

下行发布通过 FastAPI 依赖覆盖注入 ``FakePublisher``，不连接 broker；数据库
继续使用 conftest 的共享测试事务。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select

from app.messaging.mqtt.client import get_command_publisher
from app.messaging.mqtt.dedupe import MemoryDedupe
from app.messaging.mqtt.handlers import CREATED, UPDATED, handle_register
from app.models.audit import AuditLog
from app.models.device import Device, EdgeNode
from tests.conftest import OTHER_TENANT, TENANT, auth_headers

TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
TP_BODY = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
TP_HEADER = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"


class FakePublisher:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def publish_command(self, node_key: str, action: str, payload: dict, qos: int = 1):
        self.calls.append(
            {"node_key": node_key, "action": action, "payload": payload, "qos": qos}
        )
        return self.result


def _install_publisher(client, publisher: FakePublisher) -> None:
    client._transport.app.dependency_overrides[get_command_publisher] = lambda: publisher


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_node(
    session,
    node_key: str = "node-api",
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


async def _seed_device(
    session,
    node: EdgeNode,
    *,
    name: str = "cam-1",
    tenant_id=TENANT,
    config: dict | None = None,
    status: str = "online",
) -> Device:
    row = Device(
        tenant_id=tenant_id,
        node_id=node.id,
        name=name,
        kind="camera",
        config=config or {"rtsp": "rtsp://example/stream"},
        status=status,
    )
    session.add(row)
    await session.flush()
    return row


async def _node_count(session, node_key: str, tenant_id=TENANT) -> int:
    rows = (
        await session.scalars(
            select(EdgeNode.id).where(
                EdgeNode.tenant_id == tenant_id, EdgeNode.node_key == node_key
            )
        )
    ).all()
    return len(rows)


async def test_duplicate_registration_is_visible_as_one_node(
    client, session, admin_token, seeded_users
):
    key = "dup-node"
    dedupe = MemoryDedupe()
    payload = {"msg_id": "r1", "os": "linux", "arch": "x86_64", "agent_version": "0.1.0"}
    assert await handle_register(session, TENANT, key, payload, dedupe) == CREATED
    assert (
        await handle_register(
            session, TENANT, key, {**payload, "msg_id": "r2", "agent_version": "0.2.0"}, dedupe
        )
        == UPDATED
    )

    r = await client.get("/api/v1/nodes", headers=auth_headers(admin_token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["node_key"] == key
    assert data["items"][0]["agent_version"] == "0.2.0"
    assert await _node_count(session, key) == 1

    detail = await client.get(f"/api/v1/nodes/{key}", headers=auth_headers(admin_token))
    assert detail.status_code == 200
    assert detail.json()["data"]["node_key"] == key


async def test_nodes_list_status_filter_and_pagination(
    client, session, admin_token, seeded_users
):
    await _seed_node(session, "online-1", status="online")
    await _seed_node(session, "online-2", status="online")
    await _seed_node(session, "offline-1", status="offline")

    r = await client.get(
        "/api/v1/nodes?status=online", headers=auth_headers(admin_token)
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 2
    assert {item["node_key"] for item in data["items"]} == {"online-1", "online-2"}

    page = await client.get(
        "/api/v1/nodes?page=2&size=1", headers=auth_headers(admin_token)
    )
    assert page.status_code == 200
    paged = page.json()["data"]
    assert paged["total"] == 3
    assert paged["page"] == 2
    assert paged["size"] == 1
    assert len(paged["items"]) == 1


async def test_tenant_isolation_hides_other_tenant_node(
    client, session, admin_token, seeded_users
):
    await _seed_node(session, "other-only", tenant_id=OTHER_TENANT, status="online")

    listing = await client.get("/api/v1/nodes", headers=auth_headers(admin_token))
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 0

    detail = await client.get(
        "/api/v1/nodes/other-only", headers=auth_headers(admin_token)
    )
    assert detail.status_code == 404
    assert detail.json()["code"] == 404

    metrics = await client.get(
        "/api/v1/nodes/other-only/metrics", headers=auth_headers(admin_token)
    )
    assert metrics.status_code == 404
    assert metrics.json()["code"] == 404

    patched = await client.patch(
        "/api/v1/nodes/other-only",
        json={"name": "hijack"},
        headers=auth_headers(admin_token),
    )
    assert patched.status_code == 404
    assert patched.json()["code"] == 404


async def test_command_publish_uses_header_traceparent_and_audits(
    client, session, admin_token, seeded_users
):
    await _seed_node(session, "cmd-node")
    fake = FakePublisher()
    _install_publisher(client, fake)

    r = await client.post(
        "/api/v1/nodes/cmd-node/commands",
        json={"action": "start_task", "payload": {"foo": "bar"}},
        headers={**auth_headers(admin_token), "traceparent": TP_HEADER},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["accepted"] is True
    assert data["node_key"] == "cmd-node"
    assert data["action"] == "start_task"
    assert data["traceparent"] == TP_HEADER
    assert data["topic"] == "edge/cmd-node/cmd/start_task"
    assert data["task_id"]

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["node_key"] == "cmd-node"
    assert call["action"] == "start_task"
    assert call["qos"] == 1
    assert call["payload"]["foo"] == "bar"
    assert call["payload"]["task_id"] == data["task_id"]
    assert call["payload"]["traceparent"] == TP_HEADER

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == TENANT, AuditLog.action == "node.command"
        )
    )
    assert audit is not None
    assert audit.detail["task_id"] == data["task_id"]
    assert audit.detail["traceparent"] == TP_HEADER


async def test_command_body_traceparent_and_task_id_take_priority(
    client, session, admin_token, seeded_users
):
    await _seed_node(session, "cmd-node")
    fake = FakePublisher()
    _install_publisher(client, fake)

    r = await client.post(
        "/api/v1/nodes/cmd-node/commands",
        json={
            "action": "stop_task",
            "task_id": "task-explicit",
            "traceparent": TP_BODY,
        },
        headers={**auth_headers(admin_token), "traceparent": TP_HEADER},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["task_id"] == "task-explicit"
    assert data["traceparent"] == TP_BODY
    assert fake.calls[0]["payload"]["task_id"] == "task-explicit"
    assert fake.calls[0]["payload"]["traceparent"] == TP_BODY


async def test_command_generates_traceparent_when_absent(
    client, session, admin_token, seeded_users
):
    await _seed_node(session, "cmd-node")
    fake = FakePublisher()
    _install_publisher(client, fake)

    r = await client.post(
        "/api/v1/nodes/cmd-node/commands",
        json={"action": "sync_config"},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert TRACEPARENT_RE.match(data["traceparent"])
    assert TRACEPARENT_RE.match(fake.calls[0]["payload"]["traceparent"])


async def test_command_viewer_forbidden(client, session, viewer_token, seeded_users):
    await _seed_node(session, "cmd-node")
    fake = FakePublisher()
    _install_publisher(client, fake)

    r = await client.post(
        "/api/v1/nodes/cmd-node/commands",
        json={"action": "start_task"},
        headers=auth_headers(viewer_token),
    )
    assert r.status_code == 403
    assert fake.calls == []


async def test_command_not_found_disabled_and_mqtt_unavailable(
    client, session, admin_token, seeded_users
):
    fake = FakePublisher()
    _install_publisher(client, fake)

    missing = await client.post(
        "/api/v1/nodes/missing/commands",
        json={"action": "start_task"},
        headers=auth_headers(admin_token),
    )
    assert missing.status_code == 404

    await _seed_node(
        session, "locked", status="disabled", labels={"disabled": True}
    )
    disabled = await client.post(
        "/api/v1/nodes/locked/commands",
        json={"action": "start_task"},
        headers=auth_headers(admin_token),
    )
    assert disabled.status_code == 409
    assert fake.calls == []

    await _seed_node(session, "offline-mqtt", status="online")
    fake.result = False
    unavailable = await client.post(
        "/api/v1/nodes/offline-mqtt/commands",
        json={"action": "probe_device"},
        headers=auth_headers(admin_token),
    )
    assert unavailable.status_code == 503
    assert len(fake.calls) == 1


async def test_node_metrics_endpoint(client, session, admin_token, seeded_users):
    heartbeat = _now()
    await _seed_node(
        session,
        "metric-node",
        metrics={"cpu": 12.5, "mem": 33.0, "disk": 70.0},
        last_heartbeat_at=heartbeat,
    )

    r = await client.get(
        "/api/v1/nodes/metric-node/metrics", headers=auth_headers(admin_token)
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["node_key"] == "metric-node"
    assert data["status"] == "online"
    assert data["metrics"] == {"cpu": 12.5, "mem": 33.0, "disk": 70.0}
    assert data["last_heartbeat_at"] is not None


async def test_patch_node_updates_fields_and_toggles_disabled(
    client, session, admin_token, viewer_token, seeded_users
):
    await _seed_node(session, "patch-node", status="online", labels={"env": "lab"})

    r = await client.patch(
        "/api/v1/nodes/patch-node",
        json={"name": "新名字", "labels": {"room": "A1"}},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["name"] == "新名字"
    assert data["labels"] == {"env": "lab", "room": "A1"}

    disabled = await client.patch(
        "/api/v1/nodes/patch-node",
        json={"disabled": True},
        headers=auth_headers(admin_token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "disabled"
    assert disabled.json()["data"]["labels"]["disabled"] is True

    enabled = await client.patch(
        "/api/v1/nodes/patch-node",
        json={"disabled": False},
        headers=auth_headers(admin_token),
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["status"] == "offline"
    assert enabled.json()["data"]["labels"]["disabled"] is False

    forbidden = await client.patch(
        "/api/v1/nodes/patch-node",
        json={"disabled": True},
        headers=auth_headers(viewer_token),
    )
    assert forbidden.status_code == 403


async def test_device_crud_and_probe(
    client, session, admin_token, seeded_users
):
    node = await _seed_node(session, "device-node")
    fake = FakePublisher()
    _install_publisher(client, fake)

    created = await client.post(
        "/api/v1/devices",
        json={
            "node_id": node.id,
            "name": "大门摄像头",
            "kind": "camera",
            "config": {"rtsp": "rtsp://example/main"},
        },
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 200
    device = created.json()["data"]
    assert device["node_id"] == node.id
    assert device["name"] == "大门摄像头"
    assert device["status"] == "offline"

    listing = await client.get(
        f"/api/v1/devices?node_id={node.id}&status=offline",
        headers=auth_headers(admin_token),
    )
    assert listing.status_code == 200
    assert listing.json()["data"]["total"] == 1

    updated = await client.patch(
        f"/api/v1/devices/{device['id']}",
        json={"name": "大门摄像头-2", "status": "online", "config": {"rtsp": "rtsp://example/2"}},
        headers=auth_headers(admin_token),
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["name"] == "大门摄像头-2"
    assert updated.json()["data"]["status"] == "online"

    probe = await client.post(
        f"/api/v1/devices/{device['id']}/probe",
        json={"task_id": "probe-1"},
        headers={**auth_headers(admin_token), "traceparent": TP_HEADER},
    )
    assert probe.status_code == 200
    result = probe.json()["data"]
    assert result["device_id"] == device["id"]
    assert result["node_key"] == "device-node"
    assert result["task_id"] == "probe-1"
    assert result["traceparent"] == TP_HEADER
    assert result["topic"] == "edge/device-node/cmd/probe_device"

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["node_key"] == "device-node"
    assert call["action"] == "probe_device"
    assert call["payload"]["device_id"] == device["id"]
    # probe 使用设备当前配置（PATCH 后的值）。
    assert call["payload"]["config"] == {"rtsp": "rtsp://example/2"}
    assert call["payload"]["task_id"] == "probe-1"
    assert call["payload"]["traceparent"] == TP_HEADER

    audit = await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == TENANT, AuditLog.action == "device.probe"
        )
    )
    assert audit is not None
    assert audit.resource_id == device["id"]

    deleted = await client.delete(
        f"/api/v1/devices/{device['id']}", headers=auth_headers(admin_token)
    )
    assert deleted.status_code == 200
    empty = await client.get("/api/v1/devices", headers=auth_headers(admin_token))
    assert empty.json()["data"]["total"] == 0


async def test_device_create_requires_node_in_same_tenant(
    client, session, admin_token, viewer_token, seeded_users
):
    other_node = await _seed_node(
        session, "other-node", tenant_id=OTHER_TENANT, status="online"
    )

    r = await client.post(
        "/api/v1/devices",
        json={"node_id": other_node.id, "name": "cam-x"},
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 404

    own_node = await _seed_node(session, "own-node")
    other_device = await _seed_device(
        session, other_node, name="other-cam", tenant_id=OTHER_TENANT
    )
    hijack = await client.patch(
        f"/api/v1/devices/{other_device.id}",
        json={"name": "hijack"},
        headers=auth_headers(admin_token),
    )
    assert hijack.status_code == 404

    hijack_delete = await client.delete(
        f"/api/v1/devices/{other_device.id}", headers=auth_headers(admin_token)
    )
    assert hijack_delete.status_code == 404

    forbidden = await client.post(
        "/api/v1/devices",
        json={"node_id": own_node.id, "name": "cam-y"},
        headers=auth_headers(viewer_token),
    )
    assert forbidden.status_code == 403


async def test_device_probe_disabled_and_mqtt_unavailable(
    client, session, admin_token, seeded_users
):
    fake = FakePublisher()
    _install_publisher(client, fake)

    missing = await client.post(
        "/api/v1/devices/999999/probe", json={}, headers=auth_headers(admin_token)
    )
    assert missing.status_code == 404

    disabled_node = await _seed_node(
        session, "locked-node", status="disabled", labels={"disabled": True}
    )
    disabled_device = await _seed_device(session, disabled_node)
    conflict = await client.post(
        f"/api/v1/devices/{disabled_device.id}/probe",
        json={},
        headers=auth_headers(admin_token),
    )
    assert conflict.status_code == 409
    assert fake.calls == []

    node = await _seed_node(session, "probe-node")
    device = await _seed_device(session, node)
    fake.result = False
    unavailable = await client.post(
        f"/api/v1/devices/{device.id}/probe",
        json={},
        headers=auth_headers(admin_token),
    )
    assert unavailable.status_code == 503
    assert len(fake.calls) == 1
