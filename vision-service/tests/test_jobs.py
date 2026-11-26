from __future__ import annotations

import json

from app.inference import JobSpec
from app.jobs import JobManager
from fakes import ArtifactEngine, FailingEngine, SlowEngine, make_settings, write_video, wait_for


def _spec(tmp_path, task_uid: str) -> JobSpec:
    video = write_video(tmp_path / f"{task_uid}.mp4")
    return JobSpec(
        task_uid=task_uid,
        video_path=str(video),
        filename=video.name,
        params={"conf_threshold": 0.25},
    )


def test_completed_only_after_result_written(tmp_path):
    manager = JobManager(make_settings(tmp_path), ArtifactEngine())
    try:
        state = manager.submit(_spec(tmp_path, "task-ok"))
        assert state["status"] == "pending"
        state = wait_for(manager, "task-ok", {"completed"})
        assert state["result_available"] is True
        assert state["progress"] == 100
        assert state["result_sha256"]
        assert state["result_file_sha256"]
        assert state["result_bytes"] > 0
        assert state["result_schema_version"] == "vision-result/v1"
        result_path = tmp_path / "jobs" / "task-ok" / "result.json"
        assert result_path.is_file()
        assert not list((tmp_path / "jobs" / "task-ok").glob("*.tmp"))
        artifact = manager.result("task-ok")
        assert artifact["schema_version"] == "vision-result/v1"
    finally:
        manager.shutdown(wait=True)


def test_recovery_marks_pending_and_running_failed(tmp_path):
    job_dir = tmp_path / "jobs" / "task-stale"
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(
        json.dumps({"task_uid": "task-stale", "status": "running", "progress": 40, "attempt": 1}),
        encoding="utf-8",
    )
    manager = JobManager(make_settings(tmp_path), ArtifactEngine())
    try:
        state = manager.get("task-stale")
        assert state["status"] == "failed"
        assert state["error_code"] == "service_restarted"
        assert state["result_available"] is False
        persisted = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
        assert persisted["status"] == "failed"
    finally:
        manager.shutdown(wait=True)


def test_retry_failed_job_increments_attempt(tmp_path):
    manager = JobManager(make_settings(tmp_path), FailingEngine())
    try:
        manager.submit(_spec(tmp_path, "task-retry"))
        first = wait_for(manager, "task-retry", {"failed"})
        assert first["attempt"] == 1
        assert first["error_code"] == "inference_failed"
        manager.submit(_spec(tmp_path, "task-retry"))
        second = wait_for(manager, "task-retry", {"failed"})
        assert second["attempt"] == 2
    finally:
        manager.shutdown(wait=True)


def test_submit_completed_is_idempotent(tmp_path):
    engine = ArtifactEngine()
    manager = JobManager(make_settings(tmp_path), engine)
    try:
        manager.submit(_spec(tmp_path, "task-once"))
        wait_for(manager, "task-once", {"completed"})
        again = manager.submit(_spec(tmp_path, "task-once"))
        assert again["status"] == "completed"
        assert again["attempt"] == 1
    finally:
        manager.shutdown(wait=True)


def test_cancel_running_job(tmp_path):
    manager = JobManager(make_settings(tmp_path), SlowEngine())
    try:
        manager.submit(_spec(tmp_path, "task-cancel"))
        wait_for(manager, "task-cancel", {"running"})
        manager.cancel("task-cancel")
        state = wait_for(manager, "task-cancel", {"cancelled"})
        assert state["status"] == "cancelled"
        assert state["result_available"] is False
        assert not (tmp_path / "jobs" / "task-cancel" / "result.json").exists()
    finally:
        manager.shutdown(wait=True)


def test_queue_stats_reports_limits(tmp_path):
    manager = JobManager(make_settings(tmp_path, max_workers=2, max_pending=7), ArtifactEngine())
    try:
        stats = manager.queue_stats()
        assert stats["max_workers"] == 2
        assert stats["max_pending"] == 7
    finally:
        manager.shutdown(wait=True)
