"""Vision API（Task 16）集成测试：14 个路由、权限、跨租户、媒体签名与 Range。

数据库沿用 conftest 的共享事务；视频与 artifact 目录通过 ``vision_storage_dir``
指向 ``tmp_path``。vision-service 与 Agent 执行是外部边界，按用例 monkeypatch：
默认 ``offline_vision`` 让所有客户端方法立即失败，测试不连真实推理服务，
也不把 Mock 结果当作识别结果入库。

媒体 token 绑定由 Task 15 的 ``sign_media_token(tenant, task_uid, file_id)``
负责；API 测试只按契约拼 URL，不复制签名实现。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.event import VisionEvent
from app.models.run import WorkflowRun
from app.models.user import User
from app.models.vision import VisionModel, VisionResult, VisionTask
from app.services.vision_client import VisionClient, VisionServiceError
from app.services.vision_media import sign_media_token
from app.services.vision_storage import artifact_digest, sha256_file
from tests.conftest import OTHER_TENANT, PASSWORD, TENANT, auth_headers, login_token

NOW = datetime.now(timezone.utc).replace(microsecond=0)
MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42"
OPERATOR_EMAIL = "vision-operator@edgepilot.local"

MINIMAL_DSL = {
    "nodes": [
        {"id": "start", "type": "start", "timeout_ms": 1000},
        {"id": "end", "type": "end", "timeout_ms": 1000},
    ],
    "edges": [{"from": "start", "to": "end"}],
}


# --------------------------------------------------------------------- helpers


def _mp4_bytes(size: int = 4096) -> bytes:
    return MP4_MAGIC + b"\x00" * max(0, size - len(MP4_MAGIC))


def _find_key(data, key: str):
    """在响应 JSON 中找含指定字段的 dict；不冻结未在简报固定的外层包装。"""
    if isinstance(data, dict):
        if key in data:
            return data
        for value in data.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _task_payload(data: dict) -> dict:
    """上传/取消/重试常见两种包装：``data["task"]`` 或 task 直接展开。"""
    task = data.get("task")
    return task if isinstance(task, dict) else data


def _overview_scope(data: dict) -> dict:
    nested = data.get("tasks")
    return nested if isinstance(nested, dict) else data


async def _runs_for(session, task_uid: str) -> list[WorkflowRun]:
    rows = await session.scalars(
        select(WorkflowRun).where(
            WorkflowRun.tenant_id == TENANT,
            WorkflowRun.trigger_type == "event",
            WorkflowRun.trigger_payload["vision_task_uid"].astext == task_uid,
        )
    )
    return list(rows.all())


def _write_video(settings, task: VisionTask, data: bytes | None = None) -> bytes:
    path = settings.vision_storage_root / task.video_path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data if data is not None else _mp4_bytes()
    path.write_bytes(payload)
    return payload


def _probe_payload(
    *, frame_count: int = 100, duration_ms: int = 4000, fps: float = 25.0,
    width: int = 640, height: int = 360,
) -> dict:
    """与 vision-service /v1/probe 对齐的最小真实形状。"""
    return {
        "filename": "gate.mp4",
        "size_bytes": 4096,
        "width": width,
        "height": height,
        "fps": fps,
        "frame_count": frame_count,
        "duration_ms": duration_ms,
        "duration_known": True,
        "max_video_seconds": 900,
        "max_samples": 2000,
        "target_samples": 900,
        "model_name": "yolo11n",
        "model_version": "yolo11n-test",
        "weights_sha256": "c" * 64,
    }


def _job_state(task_uid: str | None = None) -> dict:
    """与 vision-service POST /v1/jobs 对齐的最小任务状态。"""
    return {
        "task_uid": task_uid,
        "status": "pending",
        "progress": 0,
        "message": "排队中",
        "attempt": 1,
        "video": {"filename": "gate.mp4", "width": 640, "height": 360, "fps": 25.0},
        "model": {"model_name": "yolo11n", "model_version": "yolo11n-test", "device": "cpu"},
    }


def _artifact_payload(task: VisionTask) -> dict:
    return {
        "schema_version": "vision-result/v1",
        "event_id": uuid.uuid4().hex,
        "task_uid": task.task_uid,
        "video": {
            "width": 640,
            "height": 360,
            "fps": 25.0,
            "duration_ms": 2000,
            "frame_count": 50,
            "sampled_every_n_frames": 5,
        },
        "model": {
            "model_name": "yolo11n",
            "model_version": "yolo11n-test",
            "device": "cpu",
        },
        "summary": {
            "processed_frames": 2,
            "detection_count": 2,
            "class_counts": {"person": 2, "car": 1},
            "peak_confidence": 0.93,
        },
        "frames": [
            {
                "frame_id": 0,
                "timestamp_ms": 0,
                "inference_ms": 12.5,
                "detections": [
                    {
                        "detection_id": "0-0",
                        "class_id": 0,
                        "class_name": "person",
                        "confidence": 0.91,
                        "bbox": {"x1": 10.0, "y1": 20.0, "x2": 30.0, "y2": 40.0},
                    },
                    {
                        "detection_id": "0-1",
                        "class_id": 2,
                        "class_name": "car",
                        "confidence": 0.87,
                        "bbox": {"x1": 50.0, "y1": 60.0, "x2": 70.0, "y2": 80.0},
                    },
                ],
            },
            {
                "frame_id": 5,
                "timestamp_ms": 200,
                "inference_ms": 11.0,
                "detections": [
                    {
                        "detection_id": "5-0",
                        "class_id": 0,
                        "class_name": "person",
                        "confidence": 0.55,
                        "bbox": {"x1": 12.0, "y1": 22.0, "x2": 32.0, "y2": 42.0},
                    }
                ],
            },
        ],
    }


async def _seed_task(session, *, tenant_id=TENANT, status="completed", **overrides) -> VisionTask:
    task_uid = overrides.pop("task_uid", str(uuid.uuid4()))
    values = {
        "tenant_id": tenant_id,
        "task_uid": task_uid,
        "dedup_key": uuid.uuid4().hex,
        "status": status,
        "source_type": "upload",
        "video_filename": "gate.mp4",
        "video_path": f"videos/{tenant_id}/{task_uid}.mp4",
        "video_sha256": uuid.uuid4().hex * 2,
        "file_size_bytes": 4096,
        "content_type": "video/mp4",
        "conf_threshold": Decimal("0.250"),
        "frame_stride": 5,
        "target_samples": 900,
        "progress": 100 if status == "completed" else 0,
        "processed_frames": 2 if status == "completed" else None,
        "detection_count": 2 if status == "completed" else 0,
        "model_name": "yolo11n",
        "model_version": "yolo11n-test",
        "device": "cpu",
        "error_code": "inference_failed" if status == "failed" else None,
        "error_message": "推理失败" if status == "failed" else None,
        "started_at": NOW,
        "finished_at": NOW if status in ("completed", "failed", "cancelled") else None,
        "dispatch_attempts": 0,
    }
    values.update(overrides)
    task = VisionTask(**values)
    session.add(task)
    await session.flush()
    return task


async def _seed_result(
    session,
    task: VisionTask,
    *,
    trigger_status: str = "pending",
    artifact_path: str | None = None,
    artifact_bytes: int | None = 256,
    artifact_file_sha256: str | None = None,
    result_sha256: str | None = "e" * 64,
    purged: bool = False,
    **overrides,
) -> VisionResult:
    values = {
        "tenant_id": task.tenant_id,
        "task_id": task.id,
        "task_uid": task.task_uid,
        # artifact_bytes / result_sha256 are NOT NULL in the model; purge only
        # clears the file pointer (spec §7.4 keeps the summary row).
        "artifact_path": None if purged else artifact_path,
        "artifact_bytes": artifact_bytes,
        "artifact_file_sha256": artifact_file_sha256,
        "result_sha256": result_sha256,
        "result_schema_version": "vision-result/v1",
        "frame_count": 2,
        "detection_count": 2,
        "class_counts": {"person": 2, "car": 1},
        "peak_confidence": Decimal("0.9300"),
        "inference_ms": 23,
        "event_count": 1,
        "summary": {
            "detection_count": 2,
            "class_counts": {"person": 2, "car": 1},
            "peak_confidence": 0.93,
            "inference_ms": 23,
            "processed_frames": 2,
        },
        "trigger_status": trigger_status,
        "purged_at": NOW if purged else None,
    }
    values.update(overrides)
    row = VisionResult(**values)
    session.add(row)
    await session.flush()
    return row


async def _seed_event(
    session,
    task_uid: str,
    *,
    tenant_id=TENANT,
    occurred_at: datetime | None = None,
    event_type: str = "vision.detection",
    **overrides,
) -> VisionEvent:
    values = {
        "tenant_id": tenant_id,
        "node_id": None,
        "device_id": None,
        "event_type": event_type,
        "severity": "medium",
        "occurred_at": occurred_at or NOW,
        "confidence": Decimal("0.9100"),
        "track_id": None,
        "bbox": {"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0},
        "model_version": "yolo11n-test",
        "payload": {
            "source": "video_upload",
            "task_uid": task_uid,
            "frame_id": 1,
            "timestamp_ms": 40,
        },
        "dedup_key": f"vision:{uuid.uuid4().hex}",
        "status": "new",
    }
    values.update(overrides)
    row = VisionEvent(**values)
    session.add(row)
    await session.flush()
    return row


def _signed_url(
    task_uid: str,
    file_id: str,
    *,
    tenant_id=TENANT,
    ttl_s: int | None = None,
) -> str:
    kwargs = {"ttl_s": ttl_s} if ttl_s is not None else {}
    token = sign_media_token(tenant_id, task_uid, file_id, **kwargs)
    return (
        f"/api/v1/vision/tasks/{task_uid}/video"
        f"?token={token.token}&expires={token.expires_at}"
    )


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def vision_settings(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "vision_storage_dir", str(tmp_path / "vision"))
    monkeypatch.setattr(settings, "vision_enabled", True)
    return settings


@pytest.fixture(autouse=True)
def offline_vision(monkeypatch):
    """默认把外部推理调用变成快速失败；需要成功的用例再覆盖具体方法。"""

    async def _unreachable(self, *args, **kwargs):
        raise VisionServiceError("推理服务不可达: test")

    for name in ("health", "models", "probe", "create_job", "cancel_job"):
        monkeypatch.setattr(VisionClient, name, _unreachable)


@pytest.fixture
async def operator_token(client, session, seeded_users):
    session.add(
        User(
            tenant_id=TENANT,
            email=OPERATOR_EMAIL,
            password_hash=hash_password(PASSWORD),
            display_name="Operator",
            role="operator",
            status="active",
        )
    )
    await session.flush()
    return await login_token(client, OPERATOR_EMAIL)


@pytest.fixture
async def completed_task(session, vision_settings):
    task = await _seed_task(session, status="completed")
    _write_video(vision_settings, task)
    await _seed_result(session, task, trigger_status="pending")
    await session.commit()
    return task


@pytest.fixture
async def artifact_task(session, vision_settings):
    task = await _seed_task(session, status="completed")
    _write_video(vision_settings, task)
    artifact = _artifact_payload(task)
    path = vision_settings.vision_storage_root / "jobs" / task.task_uid / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    row = await _seed_result(
        session,
        task,
        artifact_path=f"jobs/{task.task_uid}/result.json",
        artifact_bytes=path.stat().st_size,
        artifact_file_sha256=sha256_file(path),
        result_sha256=artifact_digest(artifact),
    )
    await session.commit()
    return task, row


@pytest.fixture
async def running_task(session, vision_settings):
    task = await _seed_task(session, status="running", dispatch_attempts=1, progress=10)
    _write_video(vision_settings, task)
    await _seed_result(session, task, trigger_status="pending")
    await session.commit()
    return task


@pytest.fixture
async def failed_task(session, vision_settings):
    task = await _seed_task(session, status="failed")
    _write_video(vision_settings, task)
    await session.commit()
    return task


@pytest.fixture
async def task_with_events(session, vision_settings):
    task = await _seed_task(session, status="completed", detection_count=2)
    _write_video(vision_settings, task)
    row = await _seed_result(session, task, trigger_status="skipped", event_count=2)
    events = [
        await _seed_event(session, task.task_uid, occurred_at=NOW - timedelta(minutes=2)),
        await _seed_event(session, task.task_uid, occurred_at=NOW - timedelta(minutes=1)),
    ]
    await session.commit()
    return task, row, events


@pytest.fixture
async def published_trigger_workflow(client, admin_token):
    name = get_settings().vision_trigger_workflow_name
    created = await client.post(
        "/api/v1/workflows",
        json={"name": name, "dsl": MINIMAL_DSL},
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 200, created.text
    workflow_id = created.json()["data"]["id"]
    published = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", headers=auth_headers(admin_token)
    )
    assert published.status_code == 200, published.text
    return workflow_id


# ----------------------------------------------------------------------- health


async def test_health_reports_reachable_and_unreachable(client, viewer_token, monkeypatch):
    async def fake_health(self):
        return {
            "status": "ok",
            "service": "edgepilot-vision",
            "version": "0.1.0",
            "model": {
                "status": "ready",
                "model_name": "yolo11n",
                "model_version": "yolo11n-test",
                "device": "cpu",
                "load_error": None,
            },
            "queue": {"pending": 0, "running": 0, "known": 0},
        }

    async def fake_models(self):
        item = {
            "provider": "ultralytics",
            "model_name": "yolo11n",
            "model_version": "yolo11n-test",
            "device": "cpu",
            "imgsz": 640,
            "license": "AGPL-3.0",
            "loaded": True,
            "load_status": "ready",
        }
        return {"models": [item], "items": [item], "count": 1}

    async def failing_health(self):
        raise VisionServiceError("推理服务不可达: test")

    async def failing_models(self):
        raise VisionServiceError("推理服务不可达: test")

    monkeypatch.setattr(VisionClient, "health", fake_health)
    monkeypatch.setattr(VisionClient, "models", fake_models)
    reachable = await client.get("/api/v1/vision/health", headers=auth_headers(viewer_token))
    assert reachable.status_code == 200
    data = reachable.json()["data"]
    assert data["reachable"] is True
    assert data["error"] is None

    # 不可达仍然 200（前端展示用），而不是 5xx。
    monkeypatch.setattr(VisionClient, "health", failing_health)
    monkeypatch.setattr(VisionClient, "models", failing_models)
    down = await client.get("/api/v1/vision/health", headers=auth_headers(viewer_token))
    assert down.status_code == 200
    body = down.json()["data"]
    assert body["reachable"] is False
    assert body["error"]


# --------------------------------------------------------------------- overview


async def test_overview_counts_tasks_and_recent(client, viewer_token, session, vision_settings):
    completed = await _seed_task(session, status="completed")
    failed = await _seed_task(session, status="failed")
    running = await _seed_task(session, status="running")
    await _seed_result(session, completed, trigger_status="triggered", event_count=2)
    await session.commit()

    response = await client.get("/api/v1/vision/overview", headers=auth_headers(viewer_token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    scope = _overview_scope(data)
    total = scope.get("total", scope.get("total_tasks"))
    assert total == 3
    assert scope["completed"] == 1
    assert scope["failed"] == 1
    recent = data.get("recent_tasks") or data.get("recent") or []
    assert len(recent) == 3
    assert {item["task_uid"] for item in recent} == {
        completed.task_uid,
        failed.task_uid,
        running.task_uid,
    }


# ----------------------------------------------------------------------- models


async def test_models_keeps_db_facts_when_service_unreachable(
    client, viewer_token, session, vision_settings
):
    session.add(
        VisionModel(
            tenant_id=TENANT,
            model_key="yolo11n",
            display_name="YOLO11n",
            provider="ultralytics",
            model_version="yolo11n-test",
            weights_filename="yolo11n.pt",
            weights_sha256="c" * 64,
            classes=["person", "car"],
            input_size=640,
            license="AGPL-3.0",
            license_url="https://www.ultralytics.com/license",
            commercial_use_notice="需遵守 AGPL-3.0 或购买商业许可",
            status="available",
        )
    )
    await session.commit()

    response = await client.get("/api/v1/vision/models", headers=auth_headers(viewer_token))
    assert response.status_code == 200, response.text
    text = json.dumps(response.json()["data"], ensure_ascii=False)
    assert "yolo11n" in text
    assert "AGPL-3.0" in text


# ----------------------------------------------------------------------- upload


async def test_admin_upload_success_and_duplicate_dedup(
    client, admin_token, session, vision_settings, monkeypatch
):
    async def fake_probe(self, video_path):
        return _probe_payload()

    async def fake_create(self, *args, **kwargs):
        task_uid = args[0] if args else kwargs.get("task_uid")
        return _job_state(task_uid)

    monkeypatch.setattr(VisionClient, "probe", fake_probe)
    monkeypatch.setattr(VisionClient, "create_job", fake_create)
    payload = _mp4_bytes()

    first = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        files={"file": ("gate.mp4", payload, "video/mp4")},
    )
    assert first.status_code == 200, first.text
    body = first.json()["data"]
    assert body["deduplicated"] is False
    task = _task_payload(body)
    assert task["video_filename"] == "gate.mp4"
    assert task["status"] in ("pending", "running")
    assert task["task_uid"]

    second = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        files={"file": ("gate.mp4", payload, "video/mp4")},
    )
    assert second.status_code == 200, second.text
    dedup = second.json()["data"]
    assert dedup["deduplicated"] is True
    assert _task_payload(dedup)["task_uid"] == task["task_uid"]

    count = await session.scalar(
        select(func.count()).select_from(VisionTask).where(VisionTask.tenant_id == TENANT)
    )
    assert count == 1
    stored = list((vision_settings.vision_storage_root / "videos").rglob("*.mp4"))
    assert len(stored) == 1


async def test_operator_can_upload(client, operator_token, vision_settings, monkeypatch):
    async def fake_probe(self, video_path):
        return _probe_payload(frame_count=10, duration_ms=400, fps=25.0, width=320, height=180)

    async def fake_create(self, *args, **kwargs):
        return _job_state(args[0] if args else kwargs.get("task_uid"))

    monkeypatch.setattr(VisionClient, "probe", fake_probe)
    monkeypatch.setattr(VisionClient, "create_job", fake_create)
    response = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(operator_token),
        files={"file": ("operator.mp4", _mp4_bytes(), "video/mp4")},
    )
    assert response.status_code == 200, response.text
    assert _task_payload(response.json()["data"])["video_filename"] == "operator.mp4"


async def test_upload_rejects_invalid_file_type(client, admin_token, vision_settings):
    response = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        files={"file": ("gate.mp4", b"definitely-not-a-video" * 4, "video/mp4")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400
    assert response.json()["data"]["reason"] == "invalid_file_type"


async def test_upload_rejects_invalid_zones(client, admin_token, vision_settings):
    response = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        data={"zones": json.dumps({"zones": [{"zone_id": "gate", "polygon": [[0.1, 0.1]]}]})},
        files={"file": ("gate.mp4", _mp4_bytes(), "video/mp4")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == 400
    assert response.json()["data"]["reason"] == "invalid_zones"


async def test_upload_too_large_is_413(client, admin_token, vision_settings, monkeypatch):
    monkeypatch.setattr(vision_settings, "vision_max_upload_mb", 1)
    response = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        files={"file": ("big.mp4", _mp4_bytes(1_200_000), "video/mp4")},
    )
    assert response.status_code == 413, response.text
    assert response.json()["code"] == 413
    assert response.json()["data"]["reason"] == "file_too_large"


async def test_upload_storage_failure_is_507(client, admin_token, tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("a regular file blocks the storage root", encoding="utf-8")
    settings = get_settings()
    monkeypatch.setattr(settings, "vision_storage_dir", str(blocked / "vision"))

    response = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        files={"file": ("gate.mp4", _mp4_bytes(), "video/mp4")},
    )
    assert response.status_code == 507, response.text
    assert response.json()["code"] == 507
    assert response.json()["data"]["reason"] == "storage_write_failed"


async def test_upload_when_vision_down_returns_failed_task(
    client, admin_token, vision_settings, monkeypatch
):
    async def fake_probe(self, video_path):
        return _probe_payload(frame_count=20, duration_ms=800)

    async def failing_create(self, *args, **kwargs):
        raise VisionServiceError("推理服务不可达: ConnectError")

    monkeypatch.setattr(VisionClient, "probe", fake_probe)
    monkeypatch.setattr(VisionClient, "create_job", failing_create)
    response = await client.post(
        "/api/v1/vision/tasks",
        headers=auth_headers(admin_token),
        files={"file": ("gate.mp4", _mp4_bytes(), "video/mp4")},
    )
    assert response.status_code == 200, response.text
    task = _task_payload(response.json()["data"])
    assert task["status"] == "failed"
    assert task["error_code"] == "vision_service_unavailable"


# ---------------------------------------------------------------- read / write


async def test_viewer_can_read_vision_routes(client, viewer_token, artifact_task, task_with_events):
    task, _ = artifact_task
    event_task, _, _ = task_with_events
    urls = [
        "/api/v1/vision/health",
        "/api/v1/vision/overview",
        "/api/v1/vision/models",
        "/api/v1/vision/tasks",
        f"/api/v1/vision/tasks/{task.task_uid}",
        f"/api/v1/vision/tasks/{task.task_uid}/results",
        f"/api/v1/vision/tasks/{event_task.task_uid}/events",
    ]
    for url in urls:
        response = await client.get(url, headers=auth_headers(viewer_token))
        assert response.status_code == 200, (url, response.text)


async def test_viewer_cannot_write_vision_routes(
    client, viewer_token, completed_task, task_with_events
):
    task, _, events = task_with_events
    refused = [
        await client.post(
            "/api/v1/vision/tasks",
            headers=auth_headers(viewer_token),
            files={"file": ("gate.mp4", _mp4_bytes(), "video/mp4")},
        ),
        await client.post(
            f"/api/v1/vision/tasks/{completed_task.task_uid}/cancel",
            headers=auth_headers(viewer_token),
        ),
        await client.post(
            f"/api/v1/vision/tasks/{task.task_uid}/retry",
            headers=auth_headers(viewer_token),
        ),
        await client.post(
            f"/api/v1/vision/tasks/{task.task_uid}/trigger",
            headers=auth_headers(viewer_token),
        ),
        await client.post(
            f"/api/v1/vision/tasks/{task.task_uid}/analyze",
            headers=auth_headers(viewer_token),
        ),
        await client.post(
            f"/api/v1/vision/events/{events[0].id}/analyze",
            headers=auth_headers(viewer_token),
        ),
    ]
    for response in refused:
        assert response.status_code == 403, response.text


async def test_operator_passes_every_write_role_guard(
    client, operator_token, running_task, failed_task, task_with_events,
    published_trigger_workflow, monkeypatch
):
    async def fake_create(self, *args, **kwargs):
        return _job_state(args[0] if args else kwargs.get("task_uid"))

    monkeypatch.setattr(VisionClient, "create_job", fake_create)
    event_task, _, events = task_with_events

    cancelled = await client.post(
        f"/api/v1/vision/tasks/{running_task.task_uid}/cancel",
        headers=auth_headers(operator_token),
    )
    assert cancelled.status_code == 200, cancelled.text

    retried = await client.post(
        f"/api/v1/vision/tasks/{failed_task.task_uid}/retry",
        headers=auth_headers(operator_token),
    )
    assert retried.status_code == 200, retried.text

    triggered = await client.post(
        f"/api/v1/vision/tasks/{event_task.task_uid}/trigger",
        headers=auth_headers(operator_token),
    )
    assert triggered.status_code == 200, triggered.text

    # operator 通过角色校验后进入业务分支：无 vision Agent 时 409，而不是 403。
    task_analyze = await client.post(
        f"/api/v1/vision/tasks/{event_task.task_uid}/analyze",
        headers=auth_headers(operator_token),
    )
    assert task_analyze.status_code == 409, task_analyze.text
    assert task_analyze.json()["data"]["reason"] == "vision_agent_missing"

    event_analyze = await client.post(
        f"/api/v1/vision/events/{events[0].id}/analyze",
        headers=auth_headers(operator_token),
    )
    assert event_analyze.status_code == 409, event_analyze.text
    assert event_analyze.json()["data"]["reason"] == "vision_agent_missing"


async def test_missing_token_is_401(client):
    response = await client.get("/api/v1/vision/tasks")
    assert response.status_code == 401
    assert response.json()["code"] == 401


# ------------------------------------------------------------------------- list


async def test_list_tasks_filters_pagination_and_no_video_url(
    client, viewer_token, session, vision_settings
):
    completed = await _seed_task(
        session, status="completed", video_filename="gate.mp4", created_at=NOW
    )
    failed = await _seed_task(
        session, status="failed", video_filename="lobby.mp4", created_at=NOW - timedelta(minutes=2)
    )
    running = await _seed_task(
        session, status="running", video_filename="gate-2.mp4", created_at=NOW - timedelta(minutes=1)
    )
    await session.commit()

    page = await client.get(
        "/api/v1/vision/tasks?page=1&size=2", headers=auth_headers(viewer_token)
    )
    assert page.status_code == 200, page.text
    body = page.json()["data"]
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["page"] == 1 and body["size"] == 2
    for item in body["items"]:
        assert "video_url" not in item
        assert "video_expires_at" not in item

    filtered = await client.get(
        "/api/v1/vision/tasks?status=failed", headers=auth_headers(viewer_token)
    )
    filtered_body = filtered.json()["data"]
    assert filtered_body["total"] == 1
    assert filtered_body["items"][0]["task_uid"] == failed.task_uid

    query = await client.get("/api/v1/vision/tasks?q=gate", headers=auth_headers(viewer_token))
    query_body = query.json()["data"]
    assert query_body["total"] == 2
    assert {completed.task_uid, running.task_uid} <= {
        item["task_uid"] for item in query_body["items"]
    }


# ----------------------------------------------------------------------- detail


async def test_task_detail_media_fields_and_missing_video(
    client, viewer_token, session, vision_settings
):
    task = await _seed_task(session, status="completed")
    _write_video(vision_settings, task)
    await _seed_result(session, task, trigger_status="triggered")
    await session.commit()

    detail = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}", headers=auth_headers(viewer_token)
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["task_uid"] == task.task_uid
    assert body["status"] == "completed"
    assert body["has_video"] is True
    assert body["artifact_purged"] is False
    assert body["video_url"].startswith(f"/api/v1/vision/tasks/{task.task_uid}/video?")
    assert "token=" in body["video_url"] and "expires=" in body["video_url"]
    assert body["video_expires_at"]

    # 文件缺失：不返回签名 URL，但详情仍可读。
    missing = await _seed_task(session, status="completed")
    await _seed_result(session, missing, trigger_status="pending")
    await session.commit()
    missing_detail = await client.get(
        f"/api/v1/vision/tasks/{missing.task_uid}", headers=auth_headers(viewer_token)
    )
    missing_body = missing_detail.json()["data"]
    assert missing_body["has_video"] is False
    assert missing_body["video_url"] is None


async def test_task_detail_marks_purged_artifact(
    client, viewer_token, session, vision_settings
):
    task = await _seed_task(session, status="completed")
    await _seed_result(session, task, trigger_status="triggered", purged=True)
    await session.commit()

    detail = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}", headers=auth_headers(viewer_token)
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()["data"]
    assert body["artifact_purged"] is True
    assert body["has_video"] is False


# ---------------------------------------------------------------------- results


async def test_results_returns_frames_and_filters(client, viewer_token, artifact_task):
    task, _ = artifact_task
    base = f"/api/v1/vision/tasks/{task.task_uid}/results"

    response = await client.get(base, headers=auth_headers(viewer_token))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["task_uid"] == task.task_uid
    assert data["has_video"] is True
    assert data["artifact_purged"] is False
    assert data["video"]["width"] == 640
    assert data["model"]["model_name"] == "yolo11n"
    assert data["query"]["total"] == 2
    assert len(data["frames"]) == 2
    assert data["frames"][0]["detections"][0]["bbox"]["x1"] == 10.0

    by_class = await client.get(
        f"{base}?class_name=car", headers=auth_headers(viewer_token)
    )
    by_class_body = by_class.json()["data"]
    assert by_class_body["query"]["total"] == 1
    assert by_class_body["frames"][0]["frame_id"] == 0

    by_conf = await client.get(
        f"{base}?min_confidence=0.9", headers=auth_headers(viewer_token)
    )
    assert by_conf.json()["data"]["query"]["total"] == 1

    by_time = await client.get(
        f"{base}?start_ms=100", headers=auth_headers(viewer_token)
    )
    by_time_body = by_time.json()["data"]
    assert by_time_body["query"]["total"] == 1
    assert by_time_body["frames"][0]["frame_id"] == 5

    window = await client.get(
        f"{base}?limit=1&offset=1", headers=auth_headers(viewer_token)
    )
    window_body = window.json()["data"]
    assert len(window_body["frames"]) == 1
    assert window_body["frames"][0]["frame_id"] == 5


async def test_results_artifact_missing_is_404(
    client, viewer_token, session, vision_settings
):
    task = await _seed_task(session, status="completed")
    await _seed_result(
        session,
        task,
        trigger_status="triggered",
        artifact_path=f"jobs/{task.task_uid}/result.json",
        artifact_bytes=123,
        artifact_file_sha256="a" * 64,
        result_sha256="b" * 64,
    )
    await session.commit()

    response = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}/results",
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 404, response.text
    assert response.json()["data"]["reason"] == "artifact_missing"


async def test_results_artifact_corrupted_is_503(
    client, viewer_token, session, vision_settings
):
    task = await _seed_task(session, status="completed")
    artifact = _artifact_payload(task)
    path = vision_settings.vision_storage_root / "jobs" / task.task_uid / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
    await _seed_result(
        session,
        task,
        trigger_status="triggered",
        artifact_path=f"jobs/{task.task_uid}/result.json",
        artifact_bytes=path.stat().st_size,
        artifact_file_sha256="0" * 64,
        result_sha256=artifact_digest(artifact),
    )
    await session.commit()

    response = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}/results",
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 503, response.text
    assert response.json()["data"]["reason"] == "artifact_corrupted"


# ----------------------------------------------------------------------- events


async def test_task_events_list_is_scoped_to_task(client, viewer_token, session, task_with_events):
    task, _, events = task_with_events
    await _seed_event(session, "another-task-uid", occurred_at=NOW)
    await session.commit()

    response = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}/events?page=1&size=1",
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == events[-1].id

    second = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}/events?page=2&size=1",
        headers=auth_headers(viewer_token),
    )
    assert second.json()["data"]["items"][0]["id"] == events[0].id


# ----------------------------------------------------------------------- cancel


async def test_cancel_marks_cancelled_when_service_unreachable(
    client, admin_token, running_task, completed_task
):
    response = await client.post(
        f"/api/v1/vision/tasks/{running_task.task_uid}/cancel",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.text
    task = _task_payload(response.json()["data"])
    assert task["status"] == "cancelled"
    assert task["error_message"]

    # 终态幂等：再次取消返回 200，不把 completed 改回 cancelled。
    again = await client.post(
        f"/api/v1/vision/tasks/{running_task.task_uid}/cancel",
        headers=auth_headers(admin_token),
    )
    assert again.status_code == 200
    terminal = await client.post(
        f"/api/v1/vision/tasks/{completed_task.task_uid}/cancel",
        headers=auth_headers(admin_token),
    )
    assert terminal.status_code == 200
    assert _task_payload(terminal.json()["data"])["status"] == "completed"


# ------------------------------------------------------------------------ retry


async def test_retry_failed_task_reuses_task(
    client, admin_token, session, failed_task, vision_settings, monkeypatch
):
    async def fake_create(self, *args, **kwargs):
        return _job_state(args[0] if args else kwargs.get("task_uid"))

    monkeypatch.setattr(VisionClient, "create_job", fake_create)
    response = await client.post(
        f"/api/v1/vision/tasks/{failed_task.task_uid}/retry",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200, response.text
    task = _task_payload(response.json()["data"])
    assert task["task_uid"] == failed_task.task_uid
    assert task["status"] in ("pending", "running")
    assert task["error_code"] is None
    assert task["dispatch_attempts"] == 1
    total = await session.scalar(
        select(func.count()).select_from(VisionTask).where(VisionTask.tenant_id == TENANT)
    )
    assert total == 1


async def test_retry_rejects_completed_and_over_limit(
    client, admin_token, session, completed_task, vision_settings
):
    not_retryable = await client.post(
        f"/api/v1/vision/tasks/{completed_task.task_uid}/retry",
        headers=auth_headers(admin_token),
    )
    assert not_retryable.status_code == 409
    assert not_retryable.json()["code"] == 409

    over_limit = await _seed_task(
        session,
        status="failed",
        dispatch_attempts=get_settings().vision_max_retries,
        error_code="inference_failed",
    )
    await session.commit()
    exceeded = await client.post(
        f"/api/v1/vision/tasks/{over_limit.task_uid}/retry",
        headers=auth_headers(admin_token),
    )
    assert exceeded.status_code == 409
    assert exceeded.json()["data"]["reason"] == "retry_limit_exceeded"


# ---------------------------------------------------------------------- trigger


async def test_trigger_route_triggers_skipped_then_reuses_run(
    client, admin_token, session, task_with_events, published_trigger_workflow
):
    task, result, _ = task_with_events
    response = await client.post(
        f"/api/v1/vision/tasks/{task.task_uid}/trigger", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200, response.text
    fields = _find_key(response.json()["data"], "trigger_status")
    assert fields is not None and fields["trigger_status"] == "triggered"

    runs = await _runs_for(session, task.task_uid)
    assert len(runs) == 1
    await session.refresh(result)
    assert result.trigger_status == "triggered"
    assert result.workflow_run_id == runs[0].id
    response_run = _find_key(response.json()["data"], "workflow_run_id")
    assert response_run is not None and response_run["workflow_run_id"] == runs[0].id

    again = await client.post(
        f"/api/v1/vision/tasks/{task.task_uid}/trigger", headers=auth_headers(admin_token)
    )
    assert again.status_code == 200, again.text
    again_fields = _find_key(again.json()["data"], "trigger_status")
    assert again_fields["trigger_status"] == "triggered"
    again_run = _find_key(again.json()["data"], "workflow_run_id")
    assert again_run is not None and again_run["workflow_run_id"] == runs[0].id
    assert len(await _runs_for(session, task.task_uid)) == 1


async def test_trigger_route_allows_failed_result(
    client, admin_token, session, published_trigger_workflow
):
    task = await _seed_task(session, status="completed")
    result = await _seed_result(session, task, trigger_status="failed")
    await _seed_event(session, task.task_uid)
    await session.commit()

    response = await client.post(
        f"/api/v1/vision/tasks/{task.task_uid}/trigger", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200, response.text
    fields = _find_key(response.json()["data"], "trigger_status")
    assert fields is not None and fields["trigger_status"] == "triggered"
    runs = await _runs_for(session, task.task_uid)
    assert len(runs) == 1
    await session.refresh(result)
    assert result.workflow_run_id == runs[0].id


async def test_trigger_route_rejects_pending_result(
    client, admin_token, session, completed_task, published_trigger_workflow
):
    response = await client.post(
        f"/api/v1/vision/tasks/{completed_task.task_uid}/trigger",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["code"] == 409


# ---------------------------------------------------------------------- analyze


async def test_analyze_task_requires_events(
    client, admin_token, session, completed_task
):
    # 即使存在 vision Agent，无事件也必须 409 no_vision_events。
    from app.models.agent import Agent

    session.add(
        Agent(tenant_id=TENANT, name="vision-agent", agent_type="vision", status="active")
    )
    await session.commit()

    response = await client.post(
        f"/api/v1/vision/tasks/{completed_task.task_uid}/analyze",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["data"]["reason"] == "no_vision_events"


async def test_analyze_task_missing_agent_is_409(client, admin_token, task_with_events):
    task, _, _ = task_with_events
    response = await client.post(
        f"/api/v1/vision/tasks/{task.task_uid}/analyze",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["data"]["reason"] == "vision_agent_missing"


async def test_analyze_success_wraps_agent_result(
    client, admin_token, task_with_events, monkeypatch
):
    task, _, events = task_with_events
    canned = {
        "id": 901,
        "agent_id": 7,
        "agent_version": 1,
        "status": "succeeded",
        "output": {"answer": "检测到人员进入"},
        "error": None,
        "trace_id": "trace-vision",
        "span_id": "span-vision",
        "total_tokens": 21,
        "total_cost": 0.0,
        "retry_count": 0,
    }

    async def fake_analyze_task(self, task_uid, user=None, **kwargs):
        assert task_uid == task.task_uid
        return dict(canned)

    async def fake_analyze_event(self, event_id, user=None, **kwargs):
        assert event_id == events[0].id
        return dict(canned)

    monkeypatch.setattr(
        "app.services.vision_agent_service.VisionAgentService.analyze_task",
        fake_analyze_task,
    )
    monkeypatch.setattr(
        "app.services.vision_agent_service.VisionAgentService.analyze_event",
        fake_analyze_event,
    )

    task_response = await client.post(
        f"/api/v1/vision/tasks/{task.task_uid}/analyze",
        headers=auth_headers(admin_token),
    )
    assert task_response.status_code == 200, task_response.text
    assert task_response.json()["data"]["status"] == "succeeded"
    assert task_response.json()["data"]["id"] == 901

    event_response = await client.post(
        f"/api/v1/vision/events/{events[0].id}/analyze",
        headers=auth_headers(admin_token),
    )
    assert event_response.status_code == 200, event_response.text
    assert event_response.json()["data"]["trace_id"] == "trace-vision"


async def test_analyze_event_missing_is_404(client, admin_token, task_with_events):
    response = await client.post(
        "/api/v1/vision/events/999999/analyze", headers=auth_headers(admin_token)
    )
    assert response.status_code == 404, response.text
    assert response.json()["data"]["reason"] == "event_not_found"


# ----------------------------------------------------------------- cross tenant


async def test_cross_tenant_access_is_404(
    client, admin_token, other_tenant_token, session, artifact_task, task_with_events
):
    my_task, _ = artifact_task
    event_task, _, events = task_with_events
    other_task = await _seed_task(session, tenant_id=OTHER_TENANT, status="completed")
    other_event = await _seed_event(
        session, other_task.task_uid, tenant_id=OTHER_TENANT
    )
    await session.commit()

    # 本租户令牌访问另一租户：统一 404。
    for url in (
        f"/api/v1/vision/tasks/{other_task.task_uid}",
        f"/api/v1/vision/tasks/{other_task.task_uid}/results",
        f"/api/v1/vision/tasks/{other_task.task_uid}/events",
    ):
        response = await client.get(url, headers=auth_headers(admin_token))
        assert response.status_code == 404, (url, response.text)
    for url in (
        f"/api/v1/vision/tasks/{other_task.task_uid}/cancel",
        f"/api/v1/vision/tasks/{other_task.task_uid}/retry",
        f"/api/v1/vision/tasks/{other_task.task_uid}/trigger",
        f"/api/v1/vision/tasks/{other_task.task_uid}/analyze",
        f"/api/v1/vision/events/{other_event.id}/analyze",
    ):
        response = await client.post(url, headers=auth_headers(admin_token))
        assert response.status_code == 404, (url, response.text)

    # 另一租户令牌访问本租户：同样 404。
    for url in (
        f"/api/v1/vision/tasks/{my_task.task_uid}",
        f"/api/v1/vision/tasks/{my_task.task_uid}/results",
        f"/api/v1/vision/tasks/{event_task.task_uid}/events",
    ):
        response = await client.get(url, headers=auth_headers(other_tenant_token))
        assert response.status_code == 404, (url, response.text)
    for url in (
        f"/api/v1/vision/tasks/{my_task.task_uid}/cancel",
        f"/api/v1/vision/tasks/{my_task.task_uid}/retry",
        f"/api/v1/vision/tasks/{my_task.task_uid}/trigger",
        f"/api/v1/vision/tasks/{my_task.task_uid}/analyze",
        f"/api/v1/vision/events/{events[0].id}/analyze",
    ):
        response = await client.post(url, headers=auth_headers(other_tenant_token))
        assert response.status_code == 404, (url, response.text)


# ---------------------------------------------------------------- vision disabled


async def test_vision_disabled_returns_503_on_every_route(
    client, admin_token, artifact_task, task_with_events, vision_settings, monkeypatch
):
    task, _ = artifact_task
    event_task, _, events = task_with_events
    detail = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}", headers=auth_headers(admin_token)
    )
    assert detail.status_code == 200, detail.text
    video_url = detail.json()["data"]["video_url"]

    monkeypatch.setattr(vision_settings, "vision_enabled", False)

    calls = [
        ("GET", "/api/v1/vision/health", {}),
        ("GET", "/api/v1/vision/overview", {}),
        ("GET", "/api/v1/vision/models", {}),
        (
            "POST",
            "/api/v1/vision/tasks",
            {"files": {"file": ("gate.mp4", _mp4_bytes(), "video/mp4")}},
        ),
        ("GET", "/api/v1/vision/tasks", {}),
        ("GET", f"/api/v1/vision/tasks/{task.task_uid}", {}),
        ("GET", f"/api/v1/vision/tasks/{task.task_uid}/results", {}),
        ("POST", f"/api/v1/vision/tasks/{task.task_uid}/cancel", {}),
        ("POST", f"/api/v1/vision/tasks/{task.task_uid}/retry", {}),
        ("POST", f"/api/v1/vision/tasks/{event_task.task_uid}/trigger", {}),
        ("GET", f"/api/v1/vision/tasks/{event_task.task_uid}/events", {}),
        ("POST", f"/api/v1/vision/tasks/{task.task_uid}/analyze", {}),
        ("POST", f"/api/v1/vision/events/{events[0].id}/analyze", {}),
        ("GET", video_url, {}),
    ]
    for method, url, kwargs in calls:
        response = await client.request(
            method, url, headers=auth_headers(admin_token), **kwargs
        )
        assert response.status_code == 503, (method, url, response.status_code, response.text)
        body = response.json()
        assert body["code"] == 503, (url, body)
        assert body["data"]["reason"] == "vision_disabled", (url, body)

    # 不影响其它路由。
    ping = await client.get("/api/v1/ping", headers=auth_headers(admin_token))
    assert ping.status_code == 200


# ------------------------------------------------------------------------ video


async def test_video_signature_and_range(client, viewer_token, artifact_task):
    task, _ = artifact_task
    detail = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}", headers=auth_headers(viewer_token)
    )
    assert detail.status_code == 200, detail.text
    video_url = detail.json()["data"]["video_url"]
    video = (get_settings().vision_storage_root / task.video_path).read_bytes()

    unsigned = await client.get(f"/api/v1/vision/tasks/{task.task_uid}/video")
    assert unsigned.status_code == 403
    assert unsigned.json()["data"]["reason"] == "invalid_media_token"

    full = await client.get(video_url)
    assert full.status_code == 200, full.text
    assert full.content == video
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-disposition"] == "inline"
    assert full.headers["cache-control"].startswith("private")

    partial = await client.get(video_url, headers={"Range": "bytes=0-1023"})
    assert partial.status_code == 206
    assert partial.content == video[:1024]
    assert partial.headers["content-range"] == f"bytes 0-1023/{len(video)}"
    assert partial.headers["content-length"] == "1024"

    out_of_range = await client.get(video_url, headers={"Range": "bytes=999999999-"})
    assert out_of_range.status_code == 416
    assert out_of_range.headers["content-range"] == f"bytes */{len(video)}"


async def test_video_token_bound_to_task_file_and_expiry(
    client, viewer_token, artifact_task
):
    task, _ = artifact_task

    # 任务绑定：合法签名但 uid 不同，不能播放本任务。
    wrong_task = await client.get(
        _signed_url("00000000-0000-0000-0000-000000000099", task.video_sha256),
        headers=auth_headers(viewer_token),
    )
    assert wrong_task.status_code == 403

    # 文件绑定：签名了另一个文件 SHA，不能播放本任务。
    wrong_file = await client.get(
        _signed_url(task.task_uid, "0" * 64), headers=auth_headers(viewer_token)
    )
    assert wrong_file.status_code == 403

    # 过期：即使签名正确也拒绝。
    expired = await client.get(
        _signed_url(task.task_uid, task.video_sha256, ttl_s=-5)
    )
    assert expired.status_code == 403
    assert expired.json()["data"]["reason"] == "invalid_media_token"

    # 跨租户签名：签名有效但不是本任务所属租户，必须拒绝（403/404 均不泄漏内容）。
    other_tenant = await client.get(
        _signed_url(task.task_uid, task.video_sha256, tenant_id=OTHER_TENANT)
    )
    assert other_tenant.status_code in (403, 404)

    # 篡改签名：保持结构但改掉最后一个字符。
    detail = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}", headers=auth_headers(viewer_token)
    )
    parsed = urlsplit(detail.json()["data"]["video_url"])
    query = parse_qs(parsed.query)
    token = query["token"][0]
    tampered = token[:-2] + ("aa" if token[-2:] != "aa" else "bb")
    tampered_url = f"{parsed.path}?token={tampered}&expires={query['expires'][0]}"
    assert (await client.get(tampered_url)).status_code == 403


async def test_video_missing_file_is_404(client, viewer_token, session, vision_settings):
    task = await _seed_task(session, status="completed")
    await session.commit()

    response = await client.get(
        _signed_url(task.task_uid, task.video_sha256), headers=auth_headers(viewer_token)
    )
    assert response.status_code == 404

