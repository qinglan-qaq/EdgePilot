"""Task 27 验收：签名视频、跨租户隔离、vision 停机隔离与真实推理门控。

真实视频推理证据只能来自 ``RUN_REAL_YOLO=1`` 门控的 Vision Task 6
（vision-service 的 ``test_inference_real.py``）。本文件不伪造、不模拟
任何 person 检测：门控关闭时相关用例只 skip；门控开启但没有 Task 6
产出的真实 artifact 时同样 skip，绝不生成假证据。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.vision import VisionResult, VisionTask
from app.services.vision_event_service import build_event_candidates
from app.services.vision_media import sign_media_token
from app.services.vision_storage import RESULT_SCHEMA_VERSION, artifact_digest
from tests.conftest import (
    ADMIN_EMAIL,
    OTHER_TENANT,
    PASSWORD,
    TENANT,
    auth_headers,
)

MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"
RUN_REAL_YOLO = os.environ.get("RUN_REAL_YOLO") == "1"


# ---------------------------------------------------------------- artifact 夹具


def _artifact(uid: str) -> dict:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "event_id": uid,
        "event_type": "vision.video_detection",
        "timestamp": "2026-11-26T10:00:00+00:00",
        "device_id": "video-upload",
        "source": {
            "type": "video_file",
            "video_id": uid,
            "filename": "gate.mp4",
            "width": 640,
            "height": 360,
            "fps": 25.0,
            "duration_ms": 2000,
        },
        "video": {
            "width": 640,
            "height": 360,
            "fps": 25.0,
            "duration_ms": 2000,
            "sampled_every_n_frames": 1,
            "processed_frames": 2,
            "timestamp_source": "fps",
            "max_samples": 2000,
        },
        "model": {
            "provider": "ultralytics",
            "model_name": "yolo11n",
            "model_version": "yolo11n-0123456789ab",
            "device": "cpu",
            "imgsz": 640,
            "license": "AGPL-3.0",
        },
        "summary": {
            "processed_frames": 2,
            "detection_count": 1,
            "class_counts": {"person": 1},
            "peak_confidence": 0.9,
            "inference_ms": 10,
        },
        "frames": [
            {
                "frame_id": 0,
                "timestamp_ms": 0,
                "inference_ms": 5.0,
                "detections": [
                    {
                        "detection_id": "0-0",
                        "class_id": 0,
                        "class_name": "person",
                        "confidence": 0.9,
                        "bbox": {"x1": 10.0, "y1": 20.0, "x2": 110.0, "y2": 220.0},
                    }
                ],
            },
            {"frame_id": 25, "timestamp_ms": 1000, "inference_ms": 5.0, "detections": []},
        ],
        "error": None,
    }


def _video_bytes() -> bytes:
    return MP4_HEADER + bytes((index * 37 + 11) % 256 for index in range(4096))


async def _seed_completed_task(session, root: Path) -> VisionTask:
    """已完成的真实文件布局：视频 + artifact + 结果行，供签名/跨租户验收。"""
    uid = str(uuid.uuid4())
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    video = _video_bytes()
    relative = f"videos/{TENANT}/{uid}.mp4"
    video_path = root / relative
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(video)

    artifact = _artifact(uid)
    artifact_path = root / "jobs" / uid / "result.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_bytes = json.dumps(artifact, ensure_ascii=False).encode("utf-8")
    artifact_path.write_bytes(artifact_bytes)

    task = VisionTask(
        tenant_id=TENANT,
        task_uid=uid,
        dedup_key=digest,
        status="completed",
        source_type="upload",
        video_filename="gate.mp4",
        video_path=relative,
        video_sha256=hashlib.sha256(video).hexdigest(),
        file_size_bytes=len(video),
        content_type="video/mp4",
        conf_threshold=Decimal("0.250"),
        frame_stride=1,
        target_samples=900,
        progress=100,
        processed_frames=2,
        detection_count=1,
        video_fps=Decimal("25.000"),
        video_duration_ms=2000,
        video_width=640,
        video_height=360,
        frame_count=50,
        sampled_every_n_frames=1,
        model_name="yolo11n",
        model_version="yolo11n-0123456789ab",
        created_by=1,
    )
    session.add(task)
    await session.flush()
    session.add(
        VisionResult(
            tenant_id=TENANT,
            task_id=task.id,
            task_uid=uid,
            artifact_path=f"jobs/{uid}/result.json",
            artifact_bytes=len(artifact_bytes),
            artifact_file_sha256=hashlib.sha256(artifact_bytes).hexdigest(),
            result_sha256=artifact_digest(artifact),
            result_schema_version=RESULT_SCHEMA_VERSION,
            frame_count=2,
            detection_count=1,
            class_counts={"person": 1},
            peak_confidence=Decimal("0.9000"),
            inference_ms=10,
            event_count=0,
            summary=artifact["summary"],
            trigger_status="skipped",
            agent_status="none",
        )
    )
    await session.flush()
    return task


def _video_url(task: VisionTask, *, ttl_s: int = 3600, tenant_id=TENANT) -> str:
    token = sign_media_token(tenant_id, task.task_uid, task.video_sha256, ttl_s=ttl_s)
    return (
        f"/api/v1/vision/tasks/{task.task_uid}/video"
        f"?expires={token.expires_at}&token={token.token}"
    )


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def tenant_context():
    token = set_tenant_id(TENANT)
    yield TENANT
    reset_tenant_id(token)


@pytest.fixture
def vision_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "vision_storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
async def completed_task(session, vision_root) -> VisionTask:
    return await _seed_completed_task(session, vision_root)


# ---------------------------------------------------------------- 真实推理门控


@pytest.mark.skipif(
    not RUN_REAL_YOLO,
    reason="真实推理证据只来自 RUN_REAL_YOLO=1 门控的 Task 6（test_inference_real.py）",
)
def test_real_video_evidence_is_consumed_from_task6_gate():
    """消费 Task 6 真实 artifact；没有真实证据时 skip，绝不生成假 person。"""
    evidence_path = os.environ.get("VISION_REAL_EVIDENCE_JSON")
    if not evidence_path:
        pytest.skip(
            "RUN_REAL_YOLO=1 但未提供 Task 6 真实 artifact 路径"
            "（VISION_REAL_EVIDENCE_JSON）；不伪造真实推理证据"
        )

    artifact = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    assert artifact.get("schema_version") == RESULT_SCHEMA_VERSION
    video = artifact.get("video") or {}
    width, height = int(video["width"]), int(video["height"])

    persons = [
        detection
        for frame in artifact.get("frames") or []
        for detection in (frame.get("detections") or [])
        if detection.get("class_name") == "person"
    ]
    assert persons, "真实视频必须至少检出 1 个 person"
    for detection in persons:
        bbox = detection["bbox"]
        assert 0 <= float(bbox["x1"]) < float(bbox["x2"]) <= width, "bbox 必须在画面内"
        assert 0 <= float(bbox["y1"]) < float(bbox["y2"]) <= height, "bbox 必须在画面内"
        assert 0.0 < float(detection["confidence"]) <= 1.0

    model = artifact.get("model") or {}
    assert model.get("model_name") == "yolo11n"
    assert model.get("license") == "AGPL-3.0"

    # 用生产事件规则消费真实 artifact：payload.simulated 固定 False。
    task = VisionTask(
        tenant_id=TENANT,
        task_uid="real-video-evidence",
        dedup_key="real-video-evidence",
        video_filename=str(video.get("filename") or "real.mp4"),
        video_path="videos/real-video-evidence.mp4",
        video_sha256="0" * 64,
        file_size_bytes=1,
        zones=None,
        model_name=model.get("model_name"),
        model_version=model.get("model_version"),
    )
    candidates = build_event_candidates(
        task, artifact, conf_threshold=0.25, cooldown_s=0
    )
    assert candidates, "真实检测必须能生成事件候选"
    assert all(candidate["payload"]["simulated"] is False for candidate in candidates)


# ---------------------------------------------------------------- 签名 URL / Range


async def test_signed_video_url_ttl_range_and_tamper_rejected(
    client, admin_token, completed_task, vision_root
):
    detail = await client.get(
        f"/api/v1/vision/tasks/{completed_task.task_uid}",
        headers=auth_headers(admin_token),
    )
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["has_video"] is True
    video_url = data.get("video_url")
    assert video_url, "任务详情必须返回 1 小时内的签名 URL"
    assert data.get("video_expires_at")

    video = (vision_root / completed_task.video_path).read_bytes()

    full = await client.get(video_url)
    assert full.status_code == 200, full.text
    assert full.content == video
    assert full.headers["accept-ranges"] == "bytes"

    ranged = await client.get(video_url, headers={"Range": "bytes=0-1023"})
    assert ranged.status_code == 206, ranged.text
    assert ranged.headers["content-range"] == f"bytes 0-1023/{len(video)}"
    assert ranged.content == video[:1024]

    bad = await client.get(video_url, headers={"Range": "bytes=999999999-"})
    assert bad.status_code == 416, bad.text
    assert bad.headers["content-range"] == f"bytes */{len(video)}"

    expired_url = _video_url(completed_task, ttl_s=-120)
    assert (await client.get(expired_url)).status_code == 403

    parsed = urlparse(video_url)
    query = parse_qs(parsed.query)
    tampered = query["token"][0]
    tampered = tampered[:-1] + ("A" if tampered[-1] != "A" else "B")
    tampered_url = parsed.path + "?" + urlencode(
        {"expires": query["expires"][0], "token": tampered}
    )
    assert (await client.get(tampered_url)).status_code == 403

    # token 绑定视频文件 SHA-256：换一个 file_id 必须 403。
    wrong_file_token = sign_media_token(
        TENANT, completed_task.task_uid, "f" * 64, ttl_s=3600
    )
    assert (
        await client.get(
            f"/api/v1/vision/tasks/{completed_task.task_uid}/video"
            f"?expires={wrong_file_token.expires_at}&token={wrong_file_token.token}"
        )
    ).status_code == 403


# ---------------------------------------------------------------- 跨租户


async def test_cross_tenant_task_events_results_and_video_are_hidden(
    client, admin_token, other_tenant_token, completed_task
):
    uid = completed_task.task_uid
    for suffix in ("", "/events", "/results"):
        response = await client.get(
            f"/api/v1/vision/tasks/{uid}{suffix}",
            headers=auth_headers(other_tenant_token),
        )
        assert response.status_code == 404, (suffix, response.text)

    # 视频路由无 Bearer，无签名一律 403。
    unsigned = await client.get(f"/api/v1/vision/tasks/{uid}/video")
    assert unsigned.status_code == 403

    # 用另一个租户签发的 token 访问本租户任务：403 或 404，绝不 200。
    foreign_url = _video_url(completed_task, tenant_id=OTHER_TENANT)
    assert (await client.get(foreign_url)).status_code in (403, 404)


# ---------------------------------------------------------------- vision 停机隔离


async def test_vision_service_down_keeps_existing_modules_available(
    client, admin_token, monkeypatch
):
    # 真实不可达地址（本机 1 端口无监听），让 VisionClient 走真实连接错误映射。
    settings = get_settings()
    monkeypatch.setattr(settings, "vision_service_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "vision_service_timeout_s", 0.5)

    health = await client.get(
        "/api/v1/vision/health", headers=auth_headers(admin_token)
    )
    assert health.status_code == 200, health.text
    assert health.json()["data"]["reachable"] is False

    # 既有模块不因推理服务不可达而受影响。
    login = await client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    for path in (
        "/api/v1/agents",
        "/api/v1/workflows",
        "/api/v1/events",
        "/api/v1/runs",
    ):
        response = await client.get(path, headers=auth_headers(admin_token))
        assert response.status_code == 200, (path, response.text)


async def test_vision_disabled_returns_503_only_for_vision_routes(
    client, admin_token, monkeypatch
):
    monkeypatch.setattr(get_settings(), "vision_enabled", False)

    blocked = await client.get(
        "/api/v1/vision/health", headers=auth_headers(admin_token)
    )
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["data"]["reason"] == "vision_disabled"

    available = await client.get("/api/v1/agents", headers=auth_headers(admin_token))
    assert available.status_code == 200, available.text
