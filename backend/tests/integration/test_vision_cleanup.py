"""Task 19：Vision 清理服务、脚本与审计集成测试（TDD red 阶段）。

需求来源：``.superpowers/sdd/2026-11-26-edgepilot-vision-plan/task-19-brief.md``。

覆盖的契约：

* 默认清理删除视频、``jobs/<uid>/result.json`` 与目录内临时工件，
  但保留 ``vision_tasks`` 汇总（status 仍为 completed）、保留 ``vision_results``
  汇总并把 ``artifact_path`` 置空、写入 ``purged_at``；
* 清理后任务仍可查询，详情标记 ``video_missing=true`` / ``artifact_purged=true``，
  结果接口不得伪造空 frames（返回 404 ``artifact_missing``）；
* ``--dry-run`` 只统计不删除，且审计记录 ``dry_run=true``；
* ``--purge-db`` 删除结果行并把任务标 ``failed(purged)``，但脚本必须同时带
  ``--confirm``，否则以退出码 2 拒绝；
* ``--actor`` 必填，审计 ``vision.cleanup`` 记录
  ``actor/task_uid/deleted_files/dry_run/purge_db``；
* HTTP 不提供清理接口，避免普通业务角色误删。

被测的 ``app.services.vision_cleanup_service`` 与 ``scripts/vision_cleanup.py``
尚未实现，当前失败属预期。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.audit import AuditLog
from app.models.vision import VisionResult, VisionTask
from tests.conftest import TENANT, auth_headers

ACTOR = "ops@edgepilot.local"
SUMMARY_MARKER = "清理后仍需保留的汇总摘要"
BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRIPT_PATH = BACKEND_DIR / "scripts" / "vision_cleanup.py"


@pytest.fixture(autouse=True)
def tenant_ctx():
    """清理服务直调需要租户上下文；HTTP 用例由请求依赖覆盖为同租户。"""
    token = set_tenant_id(TENANT)
    try:
        yield
    finally:
        reset_tenant_id(token)


@pytest.fixture
def vision_cleanup_cls():
    """延迟导入：服务未实现时让用例逐个报 ModuleNotFoundError。"""
    from app.services.vision_cleanup_service import VisionCleanupService

    return VisionCleanupService


@dataclass
class ExpiredTask:
    task: VisionTask
    result: VisionResult
    video_abs: Path
    artifact_abs: Path
    temp_abs: Path
    job_dir: Path
    summary: dict


async def _expired_task(
    session,
    root: Path,
    *,
    age_days: int = 120,
    status: str = "completed",
) -> ExpiredTask:
    """落一条过期任务，并在存储根下准备视频、result.json 与临时工件。"""
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    created_at = now - timedelta(days=age_days)

    video_rel = f"videos/{TENANT}/{uid}.mp4"
    video_abs = root / video_rel
    video_abs.parent.mkdir(parents=True, exist_ok=True)
    video_abs.write_bytes(b"fake-mp4-bytes")

    job_dir = root / "jobs" / uid
    artifact_abs = job_dir / "result.json"
    artifact_abs.parent.mkdir(parents=True, exist_ok=True)
    artifact_payload = {
        "schema_version": "vision-result/v1",
        "video": {"width": 640, "height": 360, "fps": 25.0},
        "frames": [{"frame_id": 1, "timestamp_ms": 0, "detections": []}],
    }
    artifact_abs.write_text(
        json.dumps(artifact_payload, ensure_ascii=False), encoding="utf-8"
    )
    temp_abs = job_dir / "debug" / "tmp-frames.json"
    temp_abs.parent.mkdir(parents=True, exist_ok=True)
    temp_abs.write_text("{}", encoding="utf-8")

    task = VisionTask(
        tenant_id=TENANT,
        task_uid=uid,
        dedup_key=f"vision:{uid}",
        status=status,
        source_type="upload",
        video_filename="gate.mp4",
        video_path=video_rel,
        video_sha256="b" * 64,
        file_size_bytes=len(video_abs.read_bytes()),
        conf_threshold=0.25,
        frame_stride=1,
        target_samples=900,
        progress=100 if status == "completed" else 0,
        processed_frames=2,
        detection_count=1,
        frame_count=2,
        sampled_every_n_frames=1,
        model_name="yolo11n",
        model_version="v1",
        device="cpu",
        video_fps=25,
        video_duration_ms=1000,
        video_width=640,
        video_height=360,
        started_at=created_at,
        finished_at=created_at + timedelta(seconds=10),
        created_at=created_at,
    )
    session.add(task)
    await session.flush()

    summary = {"summary": SUMMARY_MARKER, "person_count": 1, "model": "yolo11n"}
    result = VisionResult(
        tenant_id=TENANT,
        task_id=task.id,
        task_uid=uid,
        artifact_path=f"jobs/{uid}/result.json",
        artifact_bytes=artifact_abs.stat().st_size,
        artifact_file_sha256="c" * 64,
        result_sha256="d" * 64,
        result_schema_version="vision-result/v1",
        frame_count=2,
        detection_count=1,
        class_counts={"person": 1},
        peak_confidence=0.88,
        event_count=1,
        summary=summary,
        agent_status="none",
    )
    session.add(result)
    await session.flush()

    return ExpiredTask(
        task=task,
        result=result,
        video_abs=video_abs,
        artifact_abs=artifact_abs,
        temp_abs=temp_abs,
        job_dir=job_dir,
        summary=summary,
    )


async def _cleanup_audit(session, task_uid: str) -> AuditLog | None:
    return await session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == TENANT,
            AuditLog.action == "vision.cleanup",
            AuditLog.detail["task_uid"].astext == task_uid,
        )
    )


def _run_cleanup_script(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *args],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "DATABASE_URL": get_settings().test_database_url},
    )


# --------------------------------------------------------------------- 服务层


@pytest.mark.asyncio
async def test_cleanup_removes_files_and_keeps_summary(
    session, tmp_path, vision_cleanup_cls
):
    seeded = await _expired_task(session, tmp_path)

    stats = await vision_cleanup_cls(session, str(tmp_path)).run(
        days=90, dry_run=False, actor=ACTOR
    )

    # 视频 + result.json + 临时工件
    assert stats.deleted_files == 3
    assert not seeded.video_abs.exists()
    assert not seeded.artifact_abs.exists()
    assert not seeded.temp_abs.exists()
    assert not seeded.job_dir.exists()

    await session.refresh(seeded.task)
    await session.refresh(seeded.result)
    # 汇总与任务状态必须保留：清理绝不把 completed 改成 failed。
    assert seeded.task.status == "completed"
    assert seeded.task.video_filename == "gate.mp4"
    assert seeded.result.artifact_path is None
    assert seeded.result.purged_at is not None
    assert seeded.result.summary == seeded.summary
    assert seeded.result.frame_count == 2
    assert seeded.result.detection_count == 1

    audit = await _cleanup_audit(session, seeded.task.task_uid)
    assert audit is not None
    assert audit.detail["actor"] == ACTOR
    assert audit.detail["task_uid"] == seeded.task.task_uid
    assert audit.detail["deleted_files"] == 3
    assert audit.detail["dry_run"] is False
    assert audit.detail["purge_db"] is False


@pytest.mark.asyncio
async def test_dry_run_deletes_nothing_but_is_audited(
    session, tmp_path, vision_cleanup_cls
):
    seeded = await _expired_task(session, tmp_path)

    stats = await vision_cleanup_cls(session, str(tmp_path)).run(
        days=90, dry_run=True, actor=ACTOR
    )

    assert stats.deleted_files == 0
    assert seeded.video_abs.exists()
    assert seeded.artifact_abs.exists()
    assert seeded.temp_abs.exists()

    await session.refresh(seeded.task)
    await session.refresh(seeded.result)
    assert seeded.task.status == "completed"
    assert seeded.result.artifact_path == f"jobs/{seeded.task.task_uid}/result.json"
    assert seeded.result.purged_at is None

    audit = await _cleanup_audit(session, seeded.task.task_uid)
    assert audit is not None
    assert audit.detail["actor"] == ACTOR
    assert audit.detail["deleted_files"] == 0
    assert audit.detail["dry_run"] is True
    assert audit.detail["purge_db"] is False


@pytest.mark.asyncio
async def test_purge_db_removes_results_row_and_marks_task_purged(
    session, tmp_path, vision_cleanup_cls
):
    seeded = await _expired_task(session, tmp_path)

    stats = await vision_cleanup_cls(session, str(tmp_path)).run(
        days=90, dry_run=False, purge_db=True, actor=ACTOR
    )

    assert stats.deleted_files == 3
    assert not seeded.video_abs.exists()
    assert not seeded.job_dir.exists()

    remaining = await session.scalar(
        select(VisionResult).where(VisionResult.task_id == seeded.task.id)
    )
    assert remaining is None

    await session.refresh(seeded.task)
    assert seeded.task.status == "failed"
    assert seeded.task.error_code == "purged"

    audit = await _cleanup_audit(session, seeded.task.task_uid)
    assert audit is not None
    assert audit.detail["actor"] == ACTOR
    assert audit.detail["purge_db"] is True


# ------------------------------------------------------------------------ HTTP


@pytest.mark.asyncio
async def test_cleaned_task_detail_marks_missing_media_without_fake_frames(
    client, admin_token, session, tmp_path, vision_cleanup_cls
):
    seeded = await _expired_task(session, tmp_path)
    await vision_cleanup_cls(session, str(tmp_path)).run(
        days=90, dry_run=False, actor=ACTOR
    )

    r = await client.get(
        f"/api/v1/vision/tasks/{seeded.task.task_uid}",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "completed"
    assert data["has_video"] is False
    assert data["video_missing"] is True
    assert data["artifact_purged"] is True
    # 汇总仍可查询；不得伪造空 frames。
    assert data["result"]["summary"]["summary"] == SUMMARY_MARKER
    assert "frames" not in json.dumps(data, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_cleaned_task_results_returns_404_instead_of_empty_frames(
    client, admin_token, session, tmp_path, vision_cleanup_cls
):
    seeded = await _expired_task(session, tmp_path)
    await vision_cleanup_cls(session, str(tmp_path)).run(
        days=90, dry_run=False, actor=ACTOR
    )

    r = await client.get(
        f"/api/v1/vision/tasks/{seeded.task.task_uid}/results",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 404
    assert r.json()["code"] == 404
    assert r.json()["data"]["reason"] == "artifact_missing"


@pytest.mark.asyncio
async def test_purged_task_results_returns_404(
    client, admin_token, session, tmp_path, vision_cleanup_cls
):
    seeded = await _expired_task(session, tmp_path)
    await vision_cleanup_cls(session, str(tmp_path)).run(
        days=90, dry_run=False, purge_db=True, actor=ACTOR
    )

    r = await client.get(
        f"/api/v1/vision/tasks/{seeded.task.task_uid}/results",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 404
    assert r.json()["code"] == 404
    assert r.json()["data"]["reason"] == "artifact_missing"


@pytest.mark.asyncio
async def test_cleanup_has_no_http_endpoint(client, admin_token, viewer_token):
    """清理只允许运维脚本执行；HTTP 不暴露清理接口。"""
    for token in (admin_token, viewer_token):
        r = await client.post(
            "/api/v1/vision/cleanup", json={"days": 90}, headers=auth_headers(token)
        )
        assert r.status_code in (404, 405)
        r2 = await client.delete(
            "/api/v1/vision/tasks/any-task/cleanup",
            headers=auth_headers(token),
        )
        assert r2.status_code in (404, 405)


# ---------------------------------------------------------------------- 脚本


def test_purge_db_without_confirm_is_rejected():
    """--purge-db 缺 --confirm 必须以退出码 2 拒绝，且不做任何删除。"""
    assert SCRIPT_PATH.is_file(), f"缺少清理脚本: {SCRIPT_PATH}"

    completed = _run_cleanup_script(
        ["--days", "90", "--actor", ACTOR, "--purge-db", "--dry-run"]
    )

    assert completed.returncode == 2
    output = completed.stdout + completed.stderr
    assert "--confirm" in output


def test_actor_is_required():
    """--actor 必填，用于审计 vision.cleanup。"""
    assert SCRIPT_PATH.is_file(), f"缺少清理脚本: {SCRIPT_PATH}"

    completed = _run_cleanup_script(["--days", "90", "--dry-run"])

    assert completed.returncode == 2
    output = completed.stdout + completed.stderr
    assert "--actor" in output
