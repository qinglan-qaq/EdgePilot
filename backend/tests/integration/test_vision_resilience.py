"""Task 27 韧性：重复/并发上传、重启恢复、事件重放与清理后查询。

外部推理服务一律用测试内 Fake 或 Mock 状态；数据库使用真实 PostgreSQL
（并发用例使用独立连接，其余走 conftest 共享事务）。这里不产生任何
真实推理结论，也不伪造 person 检测。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.datastructures import Headers, UploadFile

from app.core.config import Settings, get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.event import VisionEvent
from app.models.run import WorkflowRun
from app.models.vision import VisionResult, VisionTask
from app.models.workflow import Workflow, WorkflowVersion
from app.services import vision_task_service as task_service_module
from app.services.vision_cleanup_service import VisionCleanupService
from app.services.vision_client import VisionServiceError
from app.services.vision_result_service import VisionResultService
from app.services.vision_storage import RESULT_SCHEMA_VERSION, artifact_digest
from app.services.vision_sync import VisionSyncPoller
from app.services.vision_task_service import VisionTaskService
from app.services.vision_trigger_service import VisionTriggerService
from app.workflow.dsl import dsl_hash
from tests.conftest import TENANT, auth_headers

MP4_HEADER = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41"
UPLOAD_URL = "/api/v1/vision/tasks"


# ---------------------------------------------------------------- 通用夹具


def _valid_mp4(size: int = 2048) -> bytes:
    filler = (uuid.uuid4().bytes * (size // 16 + 2))[: max(size, 64)]
    return MP4_HEADER + filler


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _person_artifact(uid: str) -> dict:
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


def _state_for(uid: str, artifact: dict, file_bytes: bytes) -> dict:
    return {
        "task_uid": uid,
        "status": "completed",
        "progress": 100,
        "attempt": 1,
        "result_available": True,
        "result_bytes": len(file_bytes),
        "result_file_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "result_sha256": artifact_digest(artifact),
        "result_schema_version": artifact["schema_version"],
        "processed_frames": artifact["summary"]["processed_frames"],
        "detection_count": artifact["summary"]["detection_count"],
        "video": artifact["video"],
        "model": artifact["model"],
        "summary": artifact["summary"],
        "error_code": None,
        "error_message": None,
    }


def _write_artifact(root: Path, uid: str, artifact: dict) -> tuple[Path, dict]:
    path = root / "jobs" / uid / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(artifact, ensure_ascii=False).encode("utf-8")
    path.write_bytes(data)
    return path, _state_for(uid, artifact, data)


async def _seed_task(
    session,
    *,
    status: str = "running",
    uid: str | None = None,
    created_at: datetime | None = None,
    dispatch_attempts: int = 1,
) -> VisionTask:
    uid = uid or str(uuid.uuid4())
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    task = VisionTask(
        tenant_id=TENANT,
        task_uid=uid,
        dedup_key=digest,
        status=status,
        source_type="upload",
        video_filename="gate.mp4",
        video_path=f"videos/{TENANT}/{uid}.mp4",
        video_sha256=digest,
        file_size_bytes=2048,
        content_type="video/mp4",
        conf_threshold=Decimal("0.250"),
        frame_stride=1,
        target_samples=900,
        model_name="yolo11n",
        model_version="yolo11n-0123456789ab",
        dispatch_attempts=dispatch_attempts,
        created_at=created_at,
        created_by=1,
    )
    session.add(task)
    await session.flush()
    return task


async def _seed_completed_task(
    session, root: Path, *, old: bool = False
) -> VisionTask:
    """视频 + artifact + 结果行齐全的完成任务；``old`` 用于清理阈值。"""
    uid = str(uuid.uuid4())
    task = await _seed_task(
        session,
        status="completed",
        uid=uid,
        created_at=(
            datetime.now(timezone.utc) - timedelta(days=100)
            if old
            else datetime.now(timezone.utc)
        ),
    )
    artifact = _person_artifact(uid)
    video = _valid_mp4(4096)
    video_path = root / task.video_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(video)
    artifact_path, _ = _write_artifact(root, uid, artifact)
    artifact_bytes = artifact_path.read_bytes()

    task.progress = 100
    task.processed_frames = 2
    task.detection_count = 1
    task.video_fps = Decimal("25.000")
    task.video_duration_ms = 2000
    task.video_width = 640
    task.video_height = 360
    task.frame_count = 50
    task.sampled_every_n_frames = 1
    task.finished_at = datetime.now(timezone.utc)
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


async def _complete_task(session, root: Path) -> tuple[VisionTask, VisionResult, dict]:
    uid = str(uuid.uuid4())
    artifact = _person_artifact(uid)
    task = await _seed_task(session, status="running", uid=uid)
    _, state = _write_artifact(root, uid, artifact)
    result = await VisionResultService(session).ingest(task, state)
    return task, result, state


async def _event_count(session, task_uid: str) -> int:
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


async def _run_count(session, task_uid: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.tenant_id == TENANT,
                WorkflowRun.trigger_type == "event",
                WorkflowRun.trigger_payload["vision_task_uid"].astext == task_uid,
            )
        )
        or 0
    )


# ---------------------------------------------------------------- fake 客户端


class FakeUploadVisionClient:
    """上传/派发协议替身；不产生任何推理结果。"""

    def __init__(self) -> None:
        self.ok = True
        self.probe_gate: asyncio.Barrier | None = None
        self.probe_calls: list[str] = []
        self.job_calls: list[dict] = []
        self.cancel_calls: list[str] = []

    async def probe(self, video_path: str) -> dict:
        self.probe_calls.append(str(video_path))
        if self.probe_gate is not None:
            await self.probe_gate.wait()
        if not self.ok:
            raise VisionServiceError("推理服务不可达: ConnectError")
        return {
            "filename": "gate.mp4",
            "size_bytes": 2048,
            "width": 1280,
            "height": 720,
            "fps": 25.0,
            "frame_count": 250,
            "duration_ms": 10000,
            "duration_known": True,
            "max_video_seconds": 900,
            "max_samples": 2000,
            "target_samples": 900,
            "model_name": "yolo11n",
            "model_version": "yolo11n-0123456789ab",
            "weights_sha256": "b" * 64,
        }

    async def create_job(
        self,
        task_uid: str,
        video_path: str,
        filename: str,
        video_sha256: str,
        conf_threshold: float,
        target_samples: int,
        frame_stride: int | None,
    ) -> dict:
        self.job_calls.append({"task_uid": task_uid, "video_path": str(video_path)})
        if not self.ok:
            raise VisionServiceError("推理服务不可达: ConnectError")
        return {"task_uid": task_uid, "status": "pending", "progress": 0}

    async def get_job(self, task_uid: str) -> dict:
        return {"task_uid": task_uid, "status": "pending", "progress": 0}

    async def cancel_job(self, task_uid: str) -> dict:
        self.cancel_calls.append(task_uid)
        return {"task_uid": task_uid, "status": "cancelled"}

    async def health(self) -> dict:
        return {"status": "ok" if self.ok else "down"}

    async def models(self) -> dict:
        return {"models": []}


class FakePollerVisionClient:
    """轮询协议替身；显式状态按 task_uid 返回，未配置时返回 running。"""

    def __init__(self) -> None:
        self.states: dict[str, dict] = {}
        self.get_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.create_calls: list[str] = []

    async def get_job(self, task_uid: str) -> dict:
        self.get_calls.append(task_uid)
        state = self.states.get(
            task_uid,
            {
                "task_uid": task_uid,
                "status": "running",
                "progress": 10,
                "attempt": 1,
                "result_available": False,
            },
        )
        return dict(state)

    async def create_job(self, task_uid: str, *args, **kwargs) -> dict:
        self.create_calls.append(task_uid)
        return {"task_uid": task_uid, "status": "pending", "progress": 0}

    async def cancel_job(self, task_uid: str) -> dict:
        self.cancel_calls.append(task_uid)
        return {"task_uid": task_uid, "status": "cancelled"}

    async def results(self, task_uid: str) -> dict:
        return dict(self.states.get(task_uid, {"task_uid": task_uid, "status": "running"}))


def _poller_settings(root: Path) -> Settings:
    return Settings(
        _env_file=None,
        vision_service_url="http://vision.test",
        vision_poll_interval_s=2,
        vision_poll_backoff_max_s=30,
        vision_poll_max_failures=30,
        vision_task_timeout_s=1800,
        vision_storage_dir=str(root),
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
def fake_vision(monkeypatch):
    fake = FakeUploadVisionClient()
    # 覆盖“模块级 import”与“调用时 import”两种实现写法。
    monkeypatch.setattr(task_service_module, "VisionClient", lambda *a, **kw: fake)
    monkeypatch.setattr(
        "app.services.vision_client.VisionClient", lambda *a, **kw: fake
    )
    return fake


@pytest.fixture
async def seeded_workflow(session) -> tuple[Workflow, WorkflowVersion]:
    dsl = {"nodes": [{"id": "start", "type": "start"}], "edges": []}
    workflow = Workflow(
        tenant_id=TENANT,
        name=get_settings().vision_trigger_workflow_name,
        description="韧性夹具",
        status="published",
        current_version=1,
        created_by=1,
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        tenant_id=TENANT,
        workflow_id=workflow.id,
        version=1,
        dsl=dsl,
        dsl_hash=dsl_hash(dsl),
        created_by=1,
    )
    session.add(version)
    await session.flush()
    return workflow, version


# ---------------------------------------------------------------- 重复 / 并发上传


async def _upload(client, token, data: bytes, *, filename: str = "gate.mp4"):
    return await client.post(
        UPLOAD_URL,
        headers=auth_headers(token),
        files={"file": (filename, data, "video/mp4")},
    )


async def test_duplicate_upload_returns_existing_task(
    client, session, admin_token, fake_vision, vision_root
):
    data = _valid_mp4()
    first = await _upload(client, admin_token, data)
    second = await _upload(client, admin_token, data)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_body = first.json()["data"]
    second_body = second.json()["data"]
    assert first_body["deduplicated"] is False
    assert second_body["deduplicated"] is True
    assert first_body["task"]["task_uid"] == second_body["task"]["task_uid"]

    count = await session.scalar(
        select(func.count())
        .select_from(VisionTask)
        .where(
            VisionTask.tenant_id == TENANT,
            VisionTask.video_sha256 == _sha256(data),
        )
    )
    assert count == 1, "同文件同参数只能有一个任务"
    assert len(fake_vision.job_calls) == 1, "指纹命中不得重复派发"


async def test_concurrent_same_file_creates_one_task(fake_vision, vision_root):
    """两个独立连接真实并发插入同一指纹；唯一约束兜底只留一个任务。"""
    settings = get_settings()
    data = _valid_mp4(4096)
    digest = _sha256(data)
    engine = create_async_engine(settings.test_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user = SimpleNamespace(
        id=1,
        tenant_id=TENANT,
        email="admin@edgepilot.local",
        display_name="Admin",
        role="admin",
        status="active",
    )
    fake_vision.probe_gate = asyncio.Barrier(2)

    async def upload_once():
        token = set_tenant_id(TENANT)
        try:
            async with factory() as db:
                upload = UploadFile(
                    file=io.BytesIO(data),
                    filename="gate.mp4",
                    headers=Headers({"content-type": "video/mp4"}),
                )
                return await VisionTaskService(db).create_upload(
                    upload,
                    conf_threshold=0.25,
                    frame_stride=None,
                    target_samples=900,
                    zones=None,
                    user=user,
                    ip="127.0.0.1",
                    user_agent="pytest",
                )
        finally:
            reset_tenant_id(token)

    try:
        results = await asyncio.gather(upload_once(), upload_once())
        uids = {item.task.task_uid for item in results}
        assert len(uids) == 1, "并发同文件只能落一个任务"
        assert {item.deduplicated for item in results} == {False, True}

        async with factory() as db:
            total = await db.scalar(
                select(func.count())
                .select_from(VisionTask)
                .where(
                    VisionTask.tenant_id == TENANT,
                    VisionTask.video_sha256 == digest,
                )
            )
        assert total == 1
    finally:
        async with factory() as db:
            await db.execute(
                delete(VisionTask).where(
                    VisionTask.tenant_id == TENANT,
                    VisionTask.video_sha256 == digest,
                )
            )
            await db.commit()
        await engine.dispose()


# ---------------------------------------------------------------- 重启恢复


async def test_poller_resumes_task_state_from_db_after_backend_restart(
    session, session_factory, vision_root
):
    """poller 实例重启后没有内存状态，必须从 DB 继续把 running 任务对账完成。"""
    uid = str(uuid.uuid4())
    artifact = _person_artifact(uid)
    task = await _seed_task(session, status="pending", uid=uid, dispatch_attempts=1)
    _, completed_state = _write_artifact(vision_root, uid, artifact)
    fake = FakePollerVisionClient()
    fake.states[uid] = {
        "task_uid": uid,
        "status": "running",
        "progress": 35,
        "attempt": 1,
        "result_available": False,
    }

    first_poller = VisionSyncPoller(
        session_factory, fake, _poller_settings(vision_root)
    )
    await first_poller.tick()
    task = await session.get(VisionTask, task.id, populate_existing=True)
    assert task.status == "running"
    assert fake.get_calls.count(uid) == 1

    # 模拟 API 进程重启：新 poller、无内存状态；远端此时已完成。
    fake.states[uid] = dict(completed_state)
    task.last_polled_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    await session.flush()

    second_poller = VisionSyncPoller(
        session_factory, fake, _poller_settings(vision_root)
    )
    await second_poller.tick()

    task = await session.get(VisionTask, task.id, populate_existing=True)
    assert task.status == "completed", "重启后必须从 DB 恢复并继续对账"
    assert task.progress == 100
    result = await session.scalar(
        select(VisionResult).where(
            VisionResult.tenant_id == TENANT, VisionResult.task_id == task.id
        )
    )
    assert result is not None
    assert result.task_uid == uid
    assert result.trigger_status == "skipped", "未配置工作流只影响联动，任务仍 completed"
    assert await _event_count(session, uid) == 1


async def test_service_restarted_fails_unfinished_and_keeps_completed_queryable(
    client, admin_token, session, session_factory, vision_root
):
    running = await _seed_task(session, status="running", dispatch_attempts=1)
    completed = await _seed_completed_task(session, vision_root)
    fake = FakePollerVisionClient()
    fake.states[running.task_uid] = {
        "task_uid": running.task_uid,
        "status": "failed",
        "progress": 0,
        "attempt": 1,
        "result_available": False,
        "error_code": "service_restarted",
        "error_message": "推理服务重启导致任务中断，请重试",
    }

    poller = VisionSyncPoller(session_factory, fake, _poller_settings(vision_root))
    await poller.tick()

    await session.refresh(running)
    assert running.status == "failed"
    assert running.error_code == "service_restarted"
    assert running.error_message
    assert completed.task_uid not in fake.get_calls, "已完成任务重启后不再轮询"

    detail = await client.get(
        f"/api/v1/vision/tasks/{completed.task_uid}",
        headers=auth_headers(admin_token),
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["status"] == "completed"


# ---------------------------------------------------------------- 事件重放


async def test_replayed_ingest_and_trigger_do_not_duplicate_events_or_runs(
    session, vision_root, seeded_workflow
):
    task, result, state = await _complete_task(session, vision_root)
    await session.refresh(result)
    assert result.trigger_status == "triggered"
    event_count = await _event_count(session, task.task_uid)
    run_count = await _run_count(session, task.task_uid)
    assert event_count == 1
    assert run_count == 1

    replayed = await VisionResultService(session).ingest(task, state)
    outcome = await VisionTriggerService(session).trigger(task, replayed)
    await session.refresh(task)

    assert task.status == "completed", "重放不得把任务改回 failed"
    assert await _event_count(session, task.task_uid) == event_count
    assert await _run_count(session, task.task_uid) == run_count == 1
    assert outcome.status == "triggered"
    assert outcome.workflow_run_id == result.workflow_run_id


# ---------------------------------------------------------------- 清理后查询


async def test_cleanup_keeps_summary_queryable(
    client, admin_token, session, vision_root
):
    task = await _seed_completed_task(session, vision_root, old=True)
    result = await session.scalar(
        select(VisionResult).where(
            VisionResult.tenant_id == TENANT, VisionResult.task_id == task.id
        )
    )
    assert result is not None

    stats = await VisionCleanupService(session, str(vision_root)).run(
        days=90, dry_run=False, actor="ops@edgepilot.local"
    )
    assert stats.deleted_files >= 2, "应删除视频与 artifact"

    await session.refresh(task)
    await session.refresh(result)
    assert task.status == "completed", "默认清理保留任务汇总，不得改成 failed"
    assert result.artifact_path is None
    assert result.purged_at is not None

    detail = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}",
        headers=auth_headers(admin_token),
    )
    assert detail.status_code == 200, "清理后任务仍可查询"
    data = detail.json()["data"]
    assert data["status"] == "completed"
    assert data["artifact_purged"] is True
    assert data["has_video"] is False

    results = await client.get(
        f"/api/v1/vision/tasks/{task.task_uid}/results",
        headers=auth_headers(admin_token),
    )
    assert results.status_code == 404
    assert results.json()["data"]["reason"] == "artifact_missing"
