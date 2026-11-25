"""事件查询 / ack / 快照 API 与视觉事件入库集成测试（Task 19）。

数据库继续使用 conftest 的共享事务；截图目录通过依赖覆盖指向 ``tmp_path``。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.api.v1.events import get_storage_dir
from app.models.audit import AuditLog
from app.models.event import VisionEvent
from app.vision.detector import Detection, MockDetector
from app.vision.rules import Frame, RuleEngine, ZoneRule, bucket_for
from app.vision.service import ingest_frame
from tests.conftest import OTHER_TENANT, TENANT, auth_headers

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


async def _seed_event(session, *, tenant_id=TENANT, **overrides) -> VisionEvent:
    values = {
        "tenant_id": tenant_id,
        "node_id": 1,
        "device_id": 1,
        "event_type": "zone",
        "severity": "high",
        "occurred_at": NOW,
        "confidence": Decimal("0.9"),
        "track_id": "7",
        "snapshot_path": None,
        "bbox": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0},
        "model_version": "mock-v1",
        "payload": {"zone_id": "Z1", "simulated": True},
        "dedup_key": f"seed-{uuid.uuid4()}",
        "status": "new",
    }
    values.update(overrides)
    row = VisionEvent(**values)
    session.add(row)
    await session.flush()
    return row


def _override_storage(client, tmp_path: Path) -> None:
    client._transport.app.dependency_overrides[get_storage_dir] = lambda: str(tmp_path)


def _write_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), (10, 20, 30)).save(path, "JPEG")


# ---------------------------------------------------------------- 列表 / 详情


async def test_list_events_filters_and_pagination(client, session, admin_token, seeded_users):
    await _seed_event(session, occurred_at=NOW - timedelta(hours=2))
    await _seed_event(session, occurred_at=NOW - timedelta(hours=1), event_type="line",
                      severity="low")
    await _seed_event(session, occurred_at=NOW, event_type="zone", severity="low")

    r = await client.get("/api/v1/events", headers=auth_headers(admin_token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 3
    # occurred_at desc
    assert [item["event_type"] for item in data["items"]] == ["zone", "line", "zone"]

    by_type = (
        await client.get("/api/v1/events?event_type=zone", headers=auth_headers(admin_token))
    ).json()["data"]
    assert by_type["total"] == 2

    by_severity = (
        await client.get("/api/v1/events?severity=low", headers=auth_headers(admin_token))
    ).json()["data"]
    assert by_severity["total"] == 2

    window = (
        await client.get(
            f"/api/v1/events?start={quote((NOW - timedelta(minutes=90)).isoformat())}",
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    assert window["total"] == 2

    window_end = (
        await client.get(
            f"/api/v1/events?end={quote((NOW - timedelta(minutes=90)).isoformat())}",
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    assert window_end["total"] == 1

    page1 = (
        await client.get("/api/v1/events?page=1&size=2", headers=auth_headers(admin_token))
    ).json()["data"]
    page2 = (
        await client.get("/api/v1/events?page=2&size=2", headers=auth_headers(admin_token))
    ).json()["data"]
    assert page1["total"] == 3 and len(page1["items"]) == 2
    assert page2["total"] == 3 and len(page2["items"]) == 1
    assert page1["size"] == 2 and page2["page"] == 2


async def test_event_list_is_tenant_isolated(client, session, admin_token, seeded_users):
    mine = await _seed_event(session)
    other = await _seed_event(session, tenant_id=OTHER_TENANT, dedup_key="other-1")

    data = (await client.get("/api/v1/events", headers=auth_headers(admin_token))).json()["data"]
    ids = {item["id"] for item in data["items"]}
    assert mine.id in ids
    assert other.id not in ids

    r = await client.get(f"/api/v1/events/{other.id}", headers=auth_headers(admin_token))
    assert r.status_code == 404


async def test_event_detail_and_404(client, session, admin_token, viewer_token, seeded_users):
    row = await _seed_event(session)
    r = await client.get(f"/api/v1/events/{row.id}", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    detail = r.json()["data"]
    assert detail["id"] == row.id
    assert detail["has_snapshot"] is False
    assert detail["payload"]["zone_id"] == "Z1"

    missing = await client.get("/api/v1/events/999999", headers=auth_headers(admin_token))
    assert missing.status_code == 404


# ---------------------------------------------------------------- ack


async def test_ack_permissions_and_idempotency(client, session, admin_token, viewer_token,
                                               seeded_users):
    row = await _seed_event(session)

    denied = await client.post(
        f"/api/v1/events/{row.id}/ack", headers=auth_headers(viewer_token)
    )
    assert denied.status_code == 403

    ok1 = await client.post(f"/api/v1/events/{row.id}/ack", headers=auth_headers(admin_token))
    assert ok1.status_code == 200
    assert ok1.json()["data"]["status"] == "acked"

    ok2 = await client.post(f"/api/v1/events/{row.id}/ack", headers=auth_headers(admin_token))
    assert ok2.status_code == 200
    assert ok2.json()["data"]["status"] == "acked"

    audits = (
        await session.scalars(
            select(AuditLog).where(
                AuditLog.tenant_id == TENANT,
                AuditLog.action == "event.ack",
                AuditLog.resource_id == row.id,
            )
        )
    ).all()
    assert len(audits) == 1


# ---------------------------------------------------------------- snapshot


async def test_snapshot_stream_success(client, session, admin_token, viewer_token, tmp_path,
                                       seeded_users):
    _override_storage(client, tmp_path)
    _write_jpeg(tmp_path / "snapshots/events/2026-11-25/cam-1.jpg")
    row = await _seed_event(session, snapshot_path="snapshots/events/2026-11-25/cam-1.jpg")

    listed = (await client.get("/api/v1/events", headers=auth_headers(viewer_token))).json()[
        "data"
    ]
    assert listed["items"][0]["has_snapshot"] is True

    r = await client.get(f"/api/v1/events/{row.id}/snapshot", headers=auth_headers(viewer_token))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")


async def test_snapshot_missing_and_traversal_are_404(client, session, admin_token, tmp_path,
                                                      seeded_users):
    _override_storage(client, tmp_path)
    secret = tmp_path.parent / "secret.jpg"
    _write_jpeg(secret)

    no_snapshot = await _seed_event(session, snapshot_path=None)
    missing = await _seed_event(
        session, snapshot_path="snapshots/events/2026-11-25/absent.jpg"
    )
    escaped = await _seed_event(session, snapshot_path="../secret.jpg")

    for row in (no_snapshot, missing, escaped):
        r = await client.get(f"/api/v1/events/{row.id}/snapshot", headers=auth_headers(admin_token))
        assert r.status_code == 404, row.id


# ---------------------------------------------------------------- ingest_frame


def _frame(at: datetime, detections: list[Detection]) -> Frame:
    return Frame(device_id=1, occurred_at=at, detections=detections, node_id=42)


def _det(track_id: int, x: float, y: float) -> Detection:
    return Detection("person", (x, y, 0.0, 0.0), 0.9, track_id)


async def _event_count(session, dedup_key: str) -> int:
    return await session.scalar(
        select(func.count()).select_from(VisionEvent).where(
            VisionEvent.tenant_id == TENANT, VisionEvent.dedup_key == dedup_key
        )
    )


async def test_ingest_frame_writes_event_with_mock_markers(session):
    at = NOW
    detection = _det(7, 5.0, 5.0)
    engine = RuleEngine([ZoneRule(zone_id="Z1", polygon=SQUARE)])
    rows = await ingest_frame(
        session, TENANT, _frame(at, [detection]), MockDetector(), engine
    )
    assert len(rows) == 1
    event = rows[0]
    assert event.event_type == "zone"
    assert event.device_id == 1 and event.node_id == 42
    assert event.model_version == "mock-v1"
    assert event.track_id == "7"
    assert event.status == "new"
    assert float(event.confidence) == pytest.approx(0.9)
    assert event.bbox == {"x": 5.0, "y": 5.0, "w": 0.0, "h": 0.0}
    assert event.payload["simulated"] is True and event.payload["mock"] is True
    assert event.dedup_key == f"1:zone:7:{bucket_for(at)}"


async def test_ingest_frame_dedup_key_is_idempotent(session):
    at = NOW
    frame = _frame(at, [_det(9, 5.0, 5.0)])
    key = f"1:zone:9:{bucket_for(at)}"

    first = await ingest_frame(
        session, TENANT, frame, MockDetector(),
        RuleEngine([ZoneRule(zone_id="Z1", polygon=SQUARE)]),
    )
    assert len(first) == 1

    # 模拟另一个进程再次处理同一帧：引擎重新发出同一 dedup_key，入库必须幂等跳过。
    second = await ingest_frame(
        session, TENANT, frame, MockDetector(),
        RuleEngine([ZoneRule(zone_id="Z1", polygon=SQUARE)]),
    )
    assert second == []
    assert await _event_count(session, key) == 1


async def test_ingest_frame_marks_degraded_snapshot(session, tmp_path):
    def failing_runner(cmd: list[str]) -> None:
        raise FileNotFoundError("ffmpeg 未安装")

    at = NOW
    rows = await ingest_frame(
        session,
        TENANT,
        _frame(at, [_det(11, 5.0, 5.0)]),
        MockDetector(),
        RuleEngine([ZoneRule(zone_id="Z1", polygon=SQUARE)]),
        snapshot_source="rtsp://demo/stream",
        storage_dir=tmp_path,
        snapshot_runner=failing_runner,
    )
    assert len(rows) == 1
    event = rows[0]
    assert event.payload["snapshot_degraded"] is True
    assert event.snapshot_path is not None
    assert (tmp_path / event.snapshot_path).is_file()
