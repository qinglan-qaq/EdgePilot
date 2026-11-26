"""Task 13 单元测试：事件规则、5s 冷却、区域入侵与确定性 dedup_key。

规则部分只调用纯函数 ``build_event_candidates``；写库/唯一冲突/重放使用
``generate`` 与 conftest 的共享测试事务（外层回滚，不污染 test 库）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select

from app.models.event import VisionEvent
from app.models.vision import VisionTask
from app.services.vision_event_service import (
    build_event_candidates,
    dedup_for,
    generate,
)
from tests.conftest import TENANT

BASE = datetime(2026, 11, 26, 10, 0, 0, tzinfo=timezone.utc)

# 归一化 0..1；默认 bbox (10,10)-(110,110) 在 200x200 画面上中心为 (0.3, 0.3)。
GATE = {
    "zone_id": "gate",
    "name": "东门",
    "polygon": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
}
YARD = {
    "zone_id": "yard",
    "name": "后院",
    "polygon": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
}


def _task(
    *,
    task_uid: str = "task-1",
    zones: list[dict] | None = None,
    created_at: datetime = BASE,
) -> VisionTask:
    return VisionTask(
        tenant_id=TENANT,
        task_uid=task_uid,
        dedup_key=f"dedup-{task_uid}",
        status="running",
        source_type="upload",
        video_filename="gate.mp4",
        video_path=f"videos/{TENANT}/{task_uid}.mp4",
        video_sha256="a" * 64,
        file_size_bytes=2048,
        conf_threshold=Decimal("0.250"),
        frame_stride=1,
        target_samples=900,
        progress=10,
        detection_count=0,
        zones=zones,
        model_version="yolo11n-abcdef123456",
        created_at=created_at,
    )


def _person(
    confidence: float = 0.9,
    *,
    x1: float = 10.0,
    y1: float = 10.0,
    x2: float = 110.0,
    y2: float = 110.0,
    class_id: int = 0,
    class_name: str = "person",
) -> dict:
    return {
        "detection_id": f"{class_name}-{confidence}",
        "class_id": class_id,
        "class_name": class_name,
        "confidence": confidence,
        "bbox": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
    }


def _frame(timestamp_ms: int, frame_id: int, detections: list[dict]) -> dict:
    return {
        "frame_id": frame_id,
        "timestamp_ms": timestamp_ms,
        "detections": detections,
    }


def _artifact(frames: list[dict], *, width: int = 200, height: int = 200) -> dict:
    return {
        "schema_version": "vision-result/v1",
        "video": {"width": width, "height": height, "fps": 25.0},
        "frames": frames,
    }


async def _persisted_task(
    session, *, zones: list[dict] | None = None, task_uid: str = "task-gen-1"
) -> VisionTask:
    task = _task(task_uid=task_uid, zones=zones)
    session.add(task)
    await session.flush()
    return task


async def _rows(session, task_uid: str) -> list[VisionEvent]:
    rows = await session.scalars(
        select(VisionEvent)
        .where(
            VisionEvent.tenant_id == TENANT,
            VisionEvent.payload["task_uid"].astext == task_uid,
        )
        .order_by(VisionEvent.occurred_at, VisionEvent.id)
    )
    return list(rows.all())


async def _count(session, task_uid: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(VisionEvent)
            .where(
                VisionEvent.tenant_id == TENANT,
                VisionEvent.payload["task_uid"].astext == task_uid,
            )
        )
        or 0
    )


# ---------------------------------------------------------------- 基础检测规则


def test_detection_event_for_person_only():
    task = _task()
    artifact = _artifact(
        [
            _frame(
                0,
                0,
                [
                    _person(0.92, x1=10.0, y1=20.0, x2=110.0, y2=120.0),
                    _person(0.99, class_id=2, class_name="car"),
                ],
            )
        ]
    )

    events = build_event_candidates(task, artifact)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "vision.detection"
    assert event["severity"] == "medium"
    assert event["timestamp_ms"] == 0
    assert event["frame_id"] == 0
    assert event["class_id"] == 0
    assert event["confidence"] == 0.92
    assert event["bbox"] == {"x": 10.0, "y": 20.0, "w": 100.0, "h": 100.0}
    payload = event["payload"]
    assert payload["source"] == "video_upload"
    assert payload["task_uid"] == task.task_uid
    assert payload["task_id"] is None
    assert payload["video_filename"] == "gate.mp4"
    assert payload["frame_id"] == 0
    assert payload["timestamp_ms"] == 0
    assert payload["class_name"] == "person"
    assert payload["detection_count"] == 2
    assert payload["simulated"] is False
    assert "zone_id" not in payload
    assert event["dedup_key"] == dedup_for(task.task_uid, "vision.detection", 0, 0)


def test_confidence_threshold_boundary():
    task = _task()
    artifact = _artifact(
        [
            _frame(0, 0, [_person(0.5999)]),
            _frame(1000, 1, [_person(0.99, class_id=2, class_name="car")]),
            _frame(2000, 2, [_person(0.6)]),
        ]
    )

    events = build_event_candidates(task, artifact)

    # 低于 VISION_EVENT_CONF=0.6 的 person 与非 person 都不产生事件；0.6 恰好通过。
    assert [event["timestamp_ms"] for event in events] == [2000]
    assert events[0]["confidence"] == 0.6


def test_highest_confidence_person_per_frame():
    task = _task()
    artifact = _artifact(
        [
            _frame(
                0,
                0,
                [
                    _person(0.75, x1=0.0, y1=0.0, x2=20.0, y2=20.0),
                    _person(0.95, x1=30.0, y1=40.0, x2=130.0, y2=140.0),
                    _person(0.99, class_id=2, class_name="car"),
                ],
            )
        ]
    )

    events = build_event_candidates(task, artifact)

    assert len(events) == 1
    assert events[0]["confidence"] == 0.95
    assert events[0]["bbox"] == {"x": 30.0, "y": 40.0, "w": 100.0, "h": 100.0}


def test_cooldown_keeps_first_and_recent_after_five_seconds():
    task = _task()
    frames = [
        _frame(0, 0, [_person(0.9)]),
        _frame(2000, 1, [_person(0.9)]),
        _frame(4999, 2, [_person(0.9)]),
        _frame(5000, 3, [_person(0.9)]),
        _frame(9999, 4, [_person(0.9)]),
        _frame(10000, 5, [_person(0.9)]),
    ]

    events = build_event_candidates(task, _artifact(frames))

    # 2s/4999ms 被 5s 冷却合并；恰好 5s 后重新产生。
    assert [event["timestamp_ms"] for event in events] == [0, 5000, 10000]
    assert [event["frame_id"] for event in events] == [0, 3, 5]


# ---------------------------------------------------------------- 区域入侵规则


def test_zone_intrusion_uses_bbox_center_inside_polygon():
    task = _task(zones=[GATE])
    # bbox 中心 (60,60)/200 = (0.3,0.3) 落在 gate 内。
    artifact = _artifact(
        [_frame(0, 0, [_person(0.9, x1=10.0, y1=10.0, x2=110.0, y2=110.0)])]
    )

    events = build_event_candidates(task, artifact)
    zone_events = [e for e in events if e["event_type"] == "security.zone_intrusion"]

    assert len(zone_events) == 1
    zone_event = zone_events[0]
    assert zone_event["severity"] == "high"
    assert zone_event["timestamp_ms"] == 0
    assert zone_event["payload"]["zone_id"] == "gate"
    assert zone_event["payload"]["zone_name"] == "东门"
    assert zone_event["dedup_key"] == dedup_for(
        task.task_uid, "security.zone_intrusion", 0, 0, "gate"
    )
    # 同一帧的普通检测事件仍然生成，两类事件互不替代。
    assert [e["event_type"] for e in events].count("vision.detection") == 1


def test_zone_intrusion_ignored_when_center_outside_polygon():
    task = _task(zones=[GATE])
    # bbox 中心 (10,10)/200 = (0.05,0.05) 在 gate 外。
    artifact = _artifact(
        [_frame(0, 0, [_person(0.9, x1=0.0, y1=0.0, x2=20.0, y2=20.0)])]
    )

    events = build_event_candidates(task, artifact)

    assert [e["event_type"] for e in events] == ["vision.detection"]


def test_zone_uses_highest_confidence_person_only():
    task = _task(zones=[GATE])
    artifact = _artifact(
        [
            _frame(
                0,
                0,
                [
                    # 最高置信度 person 在 gate 外；次高置信度 person 在 gate 内。
                    _person(0.95, x1=0.0, y1=0.0, x2=20.0, y2=20.0),
                    _person(0.72, x1=10.0, y1=10.0, x2=110.0, y2=110.0),
                ],
            )
        ]
    )

    events = build_event_candidates(task, artifact)

    assert [e["event_type"] for e in events] == ["vision.detection"]
    assert events[0]["confidence"] == 0.95


def test_zone_cooldown_is_independent_per_zone():
    task = _task(zones=[GATE, YARD])
    frames = [
        _frame(0, 0, [_person(0.9)]),
        _frame(2000, 1, [_person(0.9)]),
        _frame(5000, 2, [_person(0.9)]),
    ]

    events = build_event_candidates(task, _artifact(frames))
    zone_events = [e for e in events if e["event_type"] == "security.zone_intrusion"]

    # 每个 zone 独立 5s 冷却：0ms 各 1 条，2s 全部合并，5s 再各 1 条。
    assert [(e["timestamp_ms"], e["payload"]["zone_id"]) for e in zone_events] == [
        (0, "gate"),
        (0, "yard"),
        (5000, "gate"),
        (5000, "yard"),
    ]
    assert len({e["dedup_key"] for e in zone_events}) == 4


def test_no_zones_produces_no_zone_events():
    task = _task(zones=None)
    artifact = _artifact(
        [_frame(0, 0, [_person(0.9, x1=10.0, y1=10.0, x2=110.0, y2=110.0)])]
    )

    events = build_event_candidates(task, artifact)

    assert [e["event_type"] for e in events] == ["vision.detection"]


# ---------------------------------------------------------------- dedup_key


def test_dedup_key_is_deterministic_formatted_and_truncated():
    key = dedup_for("task-1", "vision.detection", 12, 0)
    assert key == "vision:task-1:vision.detection:12:0"
    assert dedup_for("task-1", "vision.detection", 12, 0) == key

    zone_key = dedup_for("task-1", "security.zone_intrusion", 12, 0, "gate")
    assert zone_key == "vision:task-1:security.zone_intrusion:12:0:zone:gate"

    long_uid = "t" * 100
    long_zone = "z" * 20
    truncated = dedup_for(long_uid, "security.zone_intrusion", 1, 0, long_zone)
    full = f"vision:{long_uid}:security.zone_intrusion:1:0:zone:{long_zone}"
    assert len(full) > 128
    assert len(truncated) == 128
    assert truncated == full[:128]


# ---------------------------------------------------------------- 写库与幂等


async def test_generate_persists_events_with_expected_db_fields(session):
    task = await _persisted_task(session, zones=[GATE], task_uid="task-gen-1")
    artifact = _artifact(
        [
            _frame(0, 0, [_person(0.9, x1=10.0, y1=20.0, x2=110.0, y2=120.0)]),
            _frame(5000, 1, [_person(0.9, x1=10.0, y1=20.0, x2=110.0, y2=120.0)]),
        ]
    )

    inserted = await generate(session, task, artifact)
    assert inserted == 4  # 2 帧 ×（detection + gate zone）

    rows = await _rows(session, task.task_uid)
    assert [row.event_type for row in rows] == [
        "vision.detection",
        "security.zone_intrusion",
        "vision.detection",
        "security.zone_intrusion",
    ]
    detection = rows[0]
    assert detection.device_id is None
    assert detection.node_id is None
    assert detection.severity == "medium"
    assert detection.bbox == {"x": 10.0, "y": 20.0, "w": 100.0, "h": 100.0}
    assert detection.payload["simulated"] is False
    assert detection.payload["task_uid"] == task.task_uid
    assert detection.status == "new"
    # occurred_at 是视频时间轴：task.created_at + timestamp_ms。
    assert detection.occurred_at == BASE
    assert rows[2].occurred_at == BASE + timedelta(milliseconds=5000)

    zone_row = rows[1]
    assert zone_row.severity == "high"
    assert zone_row.payload["zone_id"] == "gate"
    assert zone_row.payload["zone_name"] == "东门"


async def test_generate_replay_does_not_insert_duplicate_events(session):
    task = await _persisted_task(session, task_uid="task-gen-replay")
    artifact = _artifact([_frame(0, 0, [_person(0.9)])])

    assert await generate(session, task, artifact) == 1
    assert await generate(session, task, artifact) == 0

    assert await _count(session, task.task_uid) == 1


async def test_generate_skips_rows_conflicting_with_existing_dedup(session):
    task = await _persisted_task(session, task_uid="task-gen-conflict")
    artifact = _artifact(
        [
            _frame(0, 0, [_person(0.9)]),
            _frame(5000, 1, [_person(0.9)]),
        ]
    )
    first_key = build_event_candidates(task, artifact)[0]["dedup_key"]
    session.add(
        VisionEvent(
            tenant_id=TENANT,
            event_type="vision.detection",
            severity="medium",
            occurred_at=BASE,
            payload={"task_uid": task.task_uid, "simulated": False},
            dedup_key=first_key,
            status="new",
        )
    )
    await session.flush()

    inserted = await generate(session, task, artifact)

    assert inserted == 1  # 冲突行跳过，只新增第二帧事件
    assert await _count(session, task.task_uid) == 2
