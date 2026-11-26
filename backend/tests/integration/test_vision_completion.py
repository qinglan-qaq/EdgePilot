"""Task 12 集成测试：artifact 校验与「先结果、后完成」入库顺序。

真实 PostgreSQL + conftest 共享事务；artifact 写入临时存储根。推理服务与
工作流联动都用测试内 Fake，但 artifact 哈希/schema/帧数据是真实文件内容，
不伪造“已完成”的证据。

约定（plan Task 12）：
read_artifact（文件字节 SHA-256 + canonical SHA-256 + schema_version）
→ upsert_result + commit → 事件生成 + commit → task.completed + commit
→ 触发联动（只写 trigger_status / trigger_error）。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.event import VisionEvent
from app.models.vision import VisionResult, VisionTask
from app.services import vision_result_service as result_service_module
from app.services.vision_result_service import VisionResultService
from app.services.vision_storage import (
    ArtifactCorrupted,
    ArtifactMissing,
    ArtifactUnreadable,
    RESULT_SCHEMA_VERSION,
    artifact_digest,
)
from tests.conftest import TENANT


# ---------------------------------------------------------------- helpers


def _artifact(uid: str, frames: list[dict], *, processed: int | None = None,
              detection_count: int | None = None) -> dict:
    processed = len(frames) if processed is None else processed
    if detection_count is None:
        detection_count = sum(len(frame["detections"]) for frame in frames)
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
            "processed_frames": processed,
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
            "processed_frames": processed,
            "detection_count": detection_count,
            "class_counts": {"person": detection_count} if detection_count else {},
            "peak_confidence": 0.9 if detection_count else None,
            "inference_ms": 10,
        },
        "frames": frames,
        "error": None,
    }


def _frame(frame_id: int, timestamp_ms: int, detections: list[dict]) -> dict:
    return {
        "frame_id": frame_id,
        "timestamp_ms": timestamp_ms,
        "inference_ms": 5.0,
        "detections": detections,
    }


def _person() -> dict:
    return {
        "detection_id": "0-0",
        "class_id": 0,
        "class_name": "person",
        "confidence": 0.9,
        "bbox": {"x1": 10.0, "y1": 20.0, "x2": 110.0, "y2": 220.0},
    }


def _person_artifact(uid: str) -> dict:
    return _artifact(
        uid,
        [
            _frame(0, 0, [_person()]),
            _frame(25, 1000, []),
        ],
    )


def _empty_detections_artifact(uid: str) -> dict:
    return _artifact(
        uid,
        [
            _frame(0, 0, []),
            _frame(25, 1000, []),
        ],
    )


def _empty_frames_artifact(uid: str) -> dict:
    return _artifact(uid, [], processed=0, detection_count=0)


async def _make_task(session, *, uid: str | None = None, status: str = "running") -> VisionTask:
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
        created_by=1,
    )
    session.add(task)
    await session.flush()
    return task


def _state_for(uid: str, artifact: dict, file_bytes: bytes) -> dict:
    """模拟 vision-service 的 completed 状态；字段与 JobManager state 对齐。"""
    return {
        "task_uid": uid,
        "status": "completed",
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


async def _prepare(session, root: Path, artifact: dict) -> tuple[VisionTask, dict, Path]:
    task = await _make_task(session, uid=artifact["event_id"])
    path, state = _write_artifact(root, task.task_uid, artifact)
    return task, state, path


async def _result_count(session, task_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(VisionResult).where(
                VisionResult.tenant_id == TENANT, VisionResult.task_id == task_id
            )
        )
        or 0
    )


async def _event_count(session, task_uid: str) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(VisionEvent).where(
                VisionEvent.tenant_id == TENANT,
                VisionEvent.payload["task_uid"].astext == task_uid,
            )
        )
        or 0
    )


# ---------------------------------------------------------------- fakes / fixtures


class FakeTriggerOutcome:
    def __init__(self, status: str = "triggered", workflow_run_id: int | None = None,
                 error: str | None = None) -> None:
        self.status = status
        self.workflow_run_id = workflow_run_id
        self.error = error

    def __iter__(self):
        # 兼容实现方按元组解包的写法。
        return iter((self.status, self.workflow_run_id, self.error))


class FakeTriggerService:
    def __init__(self) -> None:
        self.calls: list[tuple[VisionTask, VisionResult]] = []
        self.task_status_at_call: list[str] = []
        self.error: Exception | None = None
        self.outcome = FakeTriggerOutcome()

    async def trigger(self, task, result):
        self.calls.append((task, result))
        self.task_status_at_call.append(task.status)
        if self.error is not None:
            raise self.error
        return self.outcome


@pytest.fixture(autouse=True)
def tenant_context():
    token = set_tenant_id(TENANT)
    yield
    reset_tenant_id(token)


@pytest.fixture
def vision_root(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "vision_storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def fake_trigger(monkeypatch):
    """所有用例都隔离 Task 17 真实联动，只验证 Task 12 的调用契约。"""
    fake = FakeTriggerService()
    monkeypatch.setattr(
        result_service_module, "VisionTriggerService", lambda session: fake
    )
    # 实现若在 ingest 内部延迟 import，也要命中同一个 Fake。
    try:
        trigger_module = importlib.import_module("app.services.vision_trigger_service")
    except ImportError:
        trigger_module = None
    if trigger_module is not None:
        monkeypatch.setattr(
            trigger_module, "VisionTriggerService", lambda session: fake
        )
    return fake


# ---------------------------------------------------------------- 成功入库顺序


async def test_result_committed_before_events_and_completed(
    session, vision_root, fake_trigger, monkeypatch
):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)

    commits: list[tuple[str, int, int]] = []
    original_commit = AsyncSession.commit

    async def recording_commit(self):
        if self is session:
            commits.append(
                (
                    task.status,
                    await _result_count(session, task.id),
                    await _event_count(session, task.task_uid),
                )
            )
        await original_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)

    result = await VisionResultService(session).ingest(task, state)

    # VisionResult 同时保存 canonical 与文件字节两个哈希，且大小/schema 完整。
    assert result.frame_count == 2
    assert result.detection_count == 1
    assert result.event_count == 1
    assert result.task_uid == task.task_uid
    assert result.result_sha256 == state["result_sha256"]
    assert result.artifact_file_sha256 == state["result_file_sha256"]
    assert result.result_sha256 != result.artifact_file_sha256
    assert result.artifact_bytes == state["result_bytes"]
    assert result.result_schema_version == RESULT_SCHEMA_VERSION
    assert result.artifact_path is not None

    await session.refresh(task)
    assert task.status == "completed"
    assert task.progress == 100
    assert task.finished_at is not None
    assert task.detection_count == 1
    assert task.error_code is None
    assert await _result_count(session, task.id) == 1
    assert await _event_count(session, task.task_uid) == 1

    # 提交顺序：结果先落库（task 尚未 completed），事件在 completed 之前落库。
    assert len(commits) >= 3
    first_status, first_results, first_events = commits[0]
    assert first_status != "completed"
    assert first_results == 1
    assert first_events == 0
    completed_at = next(
        index for index, (status, _, _) in enumerate(commits) if status == "completed"
    )
    assert completed_at > 0
    assert commits[completed_at][1] == 1
    assert commits[completed_at][2] == 1

    # 联动只在 completed 之后调用，并回写 trigger_status。
    assert fake_trigger.task_status_at_call == ["completed"]
    assert result.trigger_status == "triggered"


async def test_empty_detections_is_valid_and_completes(session, vision_root, fake_trigger):
    artifact = _empty_detections_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)

    result = await VisionResultService(session).ingest(task, state)

    assert result.frame_count == 2
    assert result.detection_count == 0
    assert result.event_count == 0
    await session.refresh(task)
    assert task.status == "completed"
    assert task.progress == 100
    assert task.detection_count == 0
    assert await _result_count(session, task.id) == 1
    assert await _event_count(session, task.task_uid) == 0


async def test_reingest_same_artifact_is_idempotent(session, vision_root, fake_trigger):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)
    fake_trigger.outcome = FakeTriggerOutcome("triggered", workflow_run_id=4242)
    service = VisionResultService(session)

    first = await service.ingest(task, state)
    first_result_id = first.id
    first_event_count = first.event_count
    first_finished_at = task.finished_at
    first_triggered_at = first.triggered_at
    first_workflow_run_id = first.workflow_run_id
    assert first_finished_at is not None
    assert first_triggered_at is not None
    assert first_workflow_run_id == 4242
    assert await _event_count(session, task.task_uid) == 1

    second = await service.ingest(task, state)

    assert second.id == first_result_id
    assert second.event_count == first_event_count
    assert await _result_count(session, task.id) == 1
    assert await _event_count(session, task.task_uid) == 1, "重放不得重复生成事件"
    await session.refresh(task)
    assert task.status == "completed"
    # 重放不得重复完成：完成时间、首次触发时间与运行关联都保持不变。
    assert task.finished_at == first_finished_at
    assert second.triggered_at == first_triggered_at
    assert second.workflow_run_id == first_workflow_run_id


# ---------------------------------------------------------------- artifact 校验失败


async def test_missing_artifact_marks_failed_without_empty_result(
    session, vision_root, fake_trigger
):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, path = await _prepare(session, vision_root, artifact)
    path.unlink()

    with pytest.raises(ArtifactMissing):
        await VisionResultService(session).ingest(task, state)

    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "artifact_missing"
    assert task.error_message
    assert await _result_count(session, task.id) == 0
    assert await _event_count(session, task.task_uid) == 0
    assert fake_trigger.calls == []


async def test_file_byte_hash_mismatch_is_corrupted_and_writes_no_result(
    session, vision_root, fake_trigger
):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, path = await _prepare(session, vision_root, artifact)
    # 等长替换 ": " → ":\t"：大小不变、JSON 语义不变，只有文件字节 SHA-256 变化。
    data = path.read_bytes()
    index = data.index(b": ")
    path.write_bytes(data[:index] + b":\t" + data[index + 2:])
    assert len(path.read_bytes()) == len(data)

    with pytest.raises(ArtifactCorrupted):
        await VisionResultService(session).ingest(task, state)

    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "artifact_corrupted"
    assert task.error_message
    assert await _result_count(session, task.id) == 0
    assert await _event_count(session, task.task_uid) == 0


async def test_canonical_hash_mismatch_is_corrupted_and_writes_no_result(
    session, vision_root, fake_trigger
):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)
    tampered = dict(state)
    tampered["result_sha256"] = "0" * 64

    with pytest.raises(ArtifactCorrupted):
        await VisionResultService(session).ingest(task, tampered)

    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "artifact_corrupted"
    assert await _result_count(session, task.id) == 0
    assert await _event_count(session, task.task_uid) == 0


async def test_schema_mismatch_is_unreadable_and_writes_no_result(
    session, vision_root, fake_trigger
):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)
    tampered = dict(state)
    tampered["result_schema_version"] = "vision-result/v0"

    with pytest.raises(ArtifactUnreadable):
        await VisionResultService(session).ingest(task, tampered)

    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "artifact_unreadable"
    assert await _result_count(session, task.id) == 0
    assert await _event_count(session, task.task_uid) == 0


async def test_empty_frames_is_invalid_and_never_completes(
    session, vision_root, fake_trigger
):
    artifact = _empty_frames_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)

    with pytest.raises(ArtifactUnreadable):
        await VisionResultService(session).ingest(task, state)

    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "artifact_unreadable"
    assert await _result_count(session, task.id) == 0
    assert await _event_count(session, task.task_uid) == 0
    assert fake_trigger.calls == []


# ---------------------------------------------------------------- 联动失败不回滚


async def test_trigger_failure_only_sets_trigger_status(
    session, vision_root, fake_trigger
):
    artifact = _person_artifact(str(uuid.uuid4()))
    task, state, _ = await _prepare(session, vision_root, artifact)
    fake_trigger.error = RuntimeError("workflow create failed")

    result = await VisionResultService(session).ingest(task, state)

    await session.refresh(task)
    assert task.status == "completed", "联动失败不得把任务改回 failed"
    assert task.error_code is None
    assert task.finished_at is not None
    assert await _result_count(session, task.id) == 1
    assert await _event_count(session, task.task_uid) == 1
    assert result.artifact_path is not None
    assert result.result_sha256 == state["result_sha256"]
    assert result.trigger_status == "failed"
    assert "workflow create failed" in (result.trigger_error or "")
