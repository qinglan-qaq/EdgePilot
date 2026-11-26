"""Task 8 契约测试：Vision 区域校验、配置默认值与输出 schema。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.exceptions import AppError, InsufficientStorage, PayloadTooLarge
from app.schemas import vision as vision_schemas
from app.schemas.vision import (
    VisionEventOut,
    VisionHealthOut,
    VisionOverviewOut,
    VisionResultOut,
    VisionTaskListOut,
    VisionTaskOut,
    VisionZone,
    VisionZonesIn,
)

TRIANGLE = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]

VISION_ENV_KEYS = (
    "VISION_MAX_UPLOAD_MB",
    "VISION_MAX_VIDEO_SECONDS",
    "VISION_TARGET_FRAMES",
    "VISION_MAX_SAMPLES",
    "VISION_TASK_TIMEOUT_S",
    "VISION_POLL_BACKOFF_MAX_S",
    "VISION_MODEL_KEY",
    "VISION_AGENT_ONCE_PER_TASK",
)


def _task_payload(**overrides):
    payload = {
        "id": 1,
        "task_uid": "task-1",
        "status": "completed",
        "video_filename": "gate.mp4",
        "file_size_bytes": 1024,
        "conf_threshold": 0.25,
        "frame_stride": 1,
        "target_samples": 900,
        "event_count": 3,
    }
    payload.update(overrides)
    return payload


def _result_payload(**overrides):
    payload = {"task_uid": "task-1", "agent_event_id": 7}
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------- zones


def test_zone_requires_three_points_and_range():
    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=[[0.1, 0.1], [0.2, 0.2]])
    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=[[0, 0], [1, 1], [2, 0]])


def test_zone_accepts_unit_square_boundaries_and_at_most_32_points():
    zone = VisionZone(zone_id="gate", polygon=TRIANGLE)
    assert zone.zone_id == "gate"
    assert zone.polygon == TRIANGLE

    thirty_two = [[0.0, 0.0] for _ in range(32)]
    assert len(VisionZone(zone_id="gate", polygon=thirty_two).polygon) == 32

    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=thirty_two + [[0.5, 0.5]])


@pytest.mark.parametrize(
    "polygon",
    [
        [[-0.01, 0.0], [1.0, 0.0], [1.0, 1.0]],
        [[0.0, 0.0], [1.01, 0.0], [1.0, 1.0]],
    ],
)
def test_zone_rejects_out_of_range_coordinates(polygon):
    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=polygon)


def test_zone_rejects_points_that_are_not_xy_pairs():
    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=[[0.0, 0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])


def test_zone_id_must_be_unique_within_request():
    zone = VisionZone(zone_id="gate", polygon=TRIANGLE)
    with pytest.raises(ValidationError):
        VisionZonesIn(zones=[zone, zone])

    single = VisionZonesIn(zones=[zone])
    assert [item.zone_id for item in single.zones] == ["gate"]


def test_zones_are_limited_to_ten():
    ten = [
        VisionZone(zone_id=f"zone-{index}", polygon=TRIANGLE)
        for index in range(10)
    ]
    assert len(VisionZonesIn(zones=ten).zones) == 10

    with pytest.raises(ValidationError):
        VisionZonesIn(zones=ten + [VisionZone(zone_id="zone-10", polygon=TRIANGLE)])


# ---------------------------------------------------------------- settings / errors


def test_settings_vision_limits_and_defaults(monkeypatch):
    for key in VISION_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    settings = Settings(_env_file=None)

    assert settings.vision_max_upload_mb == 200
    assert settings.vision_max_video_seconds == 900
    assert settings.vision_target_frames == 900
    assert settings.vision_max_samples == 2000
    assert settings.vision_task_timeout_s == 1800
    assert settings.vision_poll_backoff_max_s == 30
    assert settings.vision_model_key == "yolo11n"
    assert settings.vision_agent_once_per_task is True



def test_env_example_documents_vision_settings():
    """Task 8 要求 .env.example 列出全部 VISION_* 配置，避免部署时靠猜。"""
    env_file = Path(__file__).resolve().parents[2] / ".env.example"
    text = env_file.read_text(encoding="utf-8")
    for key in (
        "VISION_ENABLED",
        "VISION_SERVICE_URL",
        "VISION_SERVICE_TOKEN",
        "VISION_STORAGE_DIR",
        "VISION_MAX_UPLOAD_MB",
        "VISION_MAX_VIDEO_SECONDS",
        "VISION_TARGET_FRAMES",
        "VISION_MAX_SAMPLES",
        "VISION_TASK_TIMEOUT_S",
        "VISION_POLL_INTERVAL_S",
        "VISION_EVENT_CONF",
        "VISION_TRIGGER_ENABLED",
        "VISION_AGENT_ENABLED",
    ):
        assert key in text, key
    assert "推理服务" in text and "不影响" in text


def test_payload_too_large_and_insufficient_storage_statuses():
    assert issubclass(PayloadTooLarge, AppError)
    assert PayloadTooLarge.http_status == 413
    assert issubclass(InsufficientStorage, AppError)
    assert InsufficientStorage.http_status == 507


# ---------------------------------------------------------------- output schemas


def test_task_out_and_list_out_contract():
    task = VisionTaskOut(**_task_payload())
    assert task.task_uid == "task-1"
    assert task.status == "completed"
    assert task.detection_count == 0
    # 列表/详情都展示事件数（spec §8.2 列表字段），不能等调用方自己算。
    assert task.event_count == 3

    listing = VisionTaskListOut(items=[task], total=1, page=1, size=20)
    assert listing.total == 1
    assert isinstance(listing.items[0], VisionTaskOut)
    assert listing.items[0].task_uid == "task-1"


def test_result_out_exposes_task_uid_and_agent_event_id():
    result = VisionResultOut(**_result_payload())

    assert result.task_uid == "task-1"
    assert result.agent_event_id == 7
    assert result.trigger_status == "pending"
    assert result.agent_status == "none"


def test_task_detail_extends_task_with_video_and_result_fields():
    detail_cls = getattr(vision_schemas, "VisionTaskDetailOut")
    result = VisionResultOut(**_result_payload())
    expires_at = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)

    detail = detail_cls(
        **_task_payload(),
        has_video=True,
        artifact_purged=False,
        video_url="/api/v1/vision/tasks/task-1/video?expires=1&sig=abc",
        video_expires_at=expires_at,
        result=result,
    )

    assert isinstance(detail, VisionTaskOut)
    assert detail.has_video is True
    assert detail.artifact_purged is False
    assert detail.video_url.startswith("/api/v1/vision/tasks/task-1/video")
    assert detail.video_expires_at == expires_at
    assert detail.video_missing is False
    assert detail.result is not None
    assert detail.result.task_uid == "task-1"

    missing = detail_cls(
        **_task_payload(),
        has_video=False,
        video_missing=True,
        artifact_purged=True,
        result=None,
    )
    assert missing.video_missing is True
    assert missing.has_video is False
    assert missing.video_url is None


def test_event_overview_and_health_contracts():
    occurred_at = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    event = VisionEventOut(
        id=5,
        event_type="person_detected",
        occurred_at=occurred_at,
        dedup_key="dedup-1",
    )
    assert event.id == 5
    assert event.event_type == "person_detected"
    assert event.dedup_key == "dedup-1"

    # spec §8.2：overview 是分组对象，前端直接按 tasks/frames/... 读取。
    overview = VisionOverviewOut(
        tasks={"total": 3, "completed": 2, "running": 0, "failed": 1},
        frames={"processed_total": 100},
        detections={"total": 5},
        events={"total": 2, "unacked": 1},
        model={"status": "ready", "model_version": "yolo11n-abcdef123456", "device": "cpu"},
        recent_tasks=[VisionTaskOut(**_task_payload())],
    )
    assert overview.tasks["total"] == 3
    assert overview.frames["processed_total"] == 100
    assert overview.recent_tasks[0].task_uid == "task-1"

    health = VisionHealthOut(
        enabled=True,
        reachable=True,
        service={"status": "ok", "version": "0.1.0"},
        model={"key": "yolo11n"},
    )
    assert health.enabled is True
    assert health.reachable is True
    assert health.service["version"] == "0.1.0"
    assert health.model == {"key": "yolo11n"}

    down = VisionHealthOut(reachable=False, error="connection refused")
    assert down.reachable is False
    assert down.error == "connection refused"
