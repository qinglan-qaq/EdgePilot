"""视觉事件生成：基础检测事件与区域入侵事件。

规则与冷却完全由 artifact 的时间轴驱动；``dedup_key`` 是确定性的，重放
对账只会命中唯一约束，不会重复入库。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.event import VisionEvent
from app.models.vision import VisionTask

DETECTION_EVENT = "vision.detection"
ZONE_EVENT = "security.zone_intrusion"


def dedup_for(
    task_uid: str,
    event_type: str,
    frame_id: int,
    class_id: int,
    zone_id: str | None = None,
) -> str:
    key = f"vision:{task_uid}:{event_type}:{frame_id}:{class_id}"
    if zone_id:
        key = f"{key}:zone:{zone_id}"
    return key[:128]


def point_in_polygon(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    x, y = point
    inside = False
    count = len(polygon)
    if count < 3:
        return False
    j = count - 1
    for i in range(count):
        xi, yi = float(polygon[i][0]), float(polygon[i][1])
        xj, yj = float(polygon[j][0]), float(polygon[j][1])
        intersects = ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _event_payload(
    task: VisionTask,
    frame: dict[str, Any],
    detection: dict[str, Any],
    zone: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "source": "video_upload",
        "task_uid": task.task_uid,
        "task_id": task.id,
        "video_filename": task.video_filename,
        "frame_id": int(frame["frame_id"]),
        "timestamp_ms": int(frame["timestamp_ms"]),
        "class_name": detection["class_name"],
        "detection_count": len(frame.get("detections") or []),
        "simulated": False,
    }
    if zone is not None:
        payload["zone_id"] = zone["zone_id"]
        payload["zone_name"] = zone.get("name")
    return payload


def _candidate(
    task: VisionTask,
    frame: dict[str, Any],
    detection: dict[str, Any],
    event_type: str,
    severity: str,
    video_width: int,
    video_height: int,
    zone: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bbox = detection["bbox"]
    width = max(1, video_width)
    height = max(1, video_height)
    x1 = float(bbox["x1"])
    y1 = float(bbox["y1"])
    x2 = float(bbox["x2"])
    y2 = float(bbox["y2"])
    return {
        "event_type": event_type,
        "severity": severity,
        "timestamp_ms": int(frame["timestamp_ms"]),
        "frame_id": int(frame["frame_id"]),
        "class_id": int(detection["class_id"]),
        "confidence": float(detection["confidence"]),
        "bbox_pixel": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        "bbox": {
            "x": round(x1, 2),
            "y": round(y1, 2),
            "w": round(max(0.0, x2 - x1), 2),
            "h": round(max(0.0, y2 - y1), 2),
        },
        "center_norm": (
            (x1 + x2) / 2.0 / width,
            (y1 + y2) / 2.0 / height,
        ),
        "payload": _event_payload(task, frame, detection, zone),
        "dedup_key": dedup_for(
            task.task_uid,
            event_type,
            int(frame["frame_id"]),
            int(detection["class_id"]),
            zone["zone_id"] if zone else None,
        ),
    }


def build_event_candidates(
    task: VisionTask,
    artifact: dict[str, Any],
    *,
    conf_threshold: float | None = None,
    cooldown_s: int | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    threshold = settings.vision_event_conf if conf_threshold is None else conf_threshold
    cooldown_ms = (settings.vision_event_cooldown_s if cooldown_s is None else cooldown_s) * 1000

    video = artifact.get("video") or {}
    width = int(video.get("width") or 1)
    height = int(video.get("height") or 1)
    zones = [zone for zone in (task.zones or []) if isinstance(zone, dict)]

    frames = sorted(
        (frame for frame in artifact.get("frames") or [] if isinstance(frame, dict)),
        key=lambda frame: int(frame.get("timestamp_ms") or 0),
    )
    last_seen: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []

    for frame in frames:
        persons = [
            detection
            for detection in frame.get("detections") or []
            if detection.get("class_name") == "person"
            and float(detection.get("confidence") or 0.0) >= threshold
        ]
        if not persons:
            continue
        best = max(persons, key=lambda detection: float(detection.get("confidence") or 0.0))
        timestamp_ms = int(frame.get("timestamp_ms") or 0)

        if timestamp_ms - last_seen.get(DETECTION_EVENT, -(10**9)) >= cooldown_ms:
            last_seen[DETECTION_EVENT] = timestamp_ms
            candidates.append(
                _candidate(task, frame, best, DETECTION_EVENT, "medium", width, height)
            )

        for zone in zones:
            polygon = zone.get("polygon")
            if not isinstance(polygon, list) or len(polygon) < 3:
                continue
            center = (
                (float(best["bbox"]["x1"]) + float(best["bbox"]["x2"])) / 2.0 / max(1, width),
                (float(best["bbox"]["y1"]) + float(best["bbox"]["y2"])) / 2.0 / max(1, height),
            )
            if not point_in_polygon(center, polygon):
                continue
            zone_key = f"{ZONE_EVENT}:{zone['zone_id']}"
            if timestamp_ms - last_seen.get(zone_key, -(10**9)) >= cooldown_ms:
                last_seen[zone_key] = timestamp_ms
                candidates.append(
                    _candidate(task, frame, best, ZONE_EVENT, "high", width, height, zone)
                )
    return candidates


def _occurred_at(task: VisionTask, timestamp_ms: int) -> datetime:
    base = task.created_at or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base + timedelta(milliseconds=timestamp_ms)


async def generate(
    session: AsyncSession, task: VisionTask, artifact: dict[str, Any]
) -> int:
    """写入事件并返回本次实际新增行数；调用方决定 commit 时机。"""
    candidates = build_event_candidates(task, artifact)
    if not candidates:
        return 0
    rows = [
        {
            "tenant_id": task.tenant_id,
            "node_id": None,
            "device_id": None,
            "event_type": candidate["event_type"],
            "severity": candidate["severity"],
            "occurred_at": _occurred_at(task, candidate["timestamp_ms"]),
            "confidence": candidate["confidence"],
            "track_id": None,
            "bbox": candidate["bbox"],
            "model_version": (task.model_version or "")[:32] or None,
            "payload": candidate["payload"],
            "dedup_key": candidate["dedup_key"],
            "status": "new",
        }
        for candidate in candidates
    ]
    statement = (
        pg_insert(VisionEvent)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["tenant_id", "dedup_key"])
        .returning(VisionEvent.id)
    )
    result = await session.execute(statement)
    return len(result.scalars().all())
