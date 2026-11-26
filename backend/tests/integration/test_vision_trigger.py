"""Vision 工作流任务级幂等触发（Task 17）集成测试。

覆盖：一次触发只建一条 ``trigger_type='event'`` 的 Run；重放/轮询/上传命中
既有任务/重启补派发都不新增；无发布工作流或任务条件不满足时 ``skipped``；
Run 创建失败只落 ``trigger_status/trigger_error``，任务保持 completed；
已有 Run 直接复用；数据库部分唯一索引兜底并发触发。

测试直接用 service 层与真实数据库事务；工作流通过既有 API 创建并发布，
Celery 在 conftest 中已关闭，创建 Run 不会真正执行。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.event import VisionEvent
from app.models.run import WorkflowRun
from app.models.vision import VisionResult, VisionTask
from app.models.workflow import WorkflowVersion
from app.repositories.vision_repo import VisionRepo
from app.repositories.workflow_repo import WorkflowRepo
from app.services.vision_trigger_service import VisionTriggerService
from tests.conftest import OTHER_TENANT, TENANT, auth_headers

NOW = datetime.now(timezone.utc).replace(microsecond=0)

MINIMAL_DSL = {
    "nodes": [
        {"id": "start", "type": "start", "timeout_ms": 1000},
        {"id": "end", "type": "end", "timeout_ms": 1000},
    ],
    "edges": [{"from": "start", "to": "end"}],
}


# --------------------------------------------------------------------- helpers


async def _seed_task(
    session,
    *,
    tenant_id=TENANT,
    status: str = "completed",
    trigger_status: str = "pending",
    trigger_error: str | None = None,
    event_count: int = 2,
    with_events: bool = True,
) -> tuple[VisionTask, VisionResult, list[VisionEvent]]:
    task_uid = str(uuid.uuid4())
    task = VisionTask(
        tenant_id=tenant_id,
        task_uid=task_uid,
        dedup_key=uuid.uuid4().hex,
        status=status,
        source_type="upload",
        video_filename="gate.mp4",
        video_path=f"videos/{tenant_id}/{task_uid}.mp4",
        video_sha256=uuid.uuid4().hex * 2,
        file_size_bytes=4096,
        content_type="video/mp4",
        conf_threshold=Decimal("0.250"),
        frame_stride=5,
        target_samples=900,
        progress=100 if status == "completed" else 10,
        processed_frames=2 if status == "completed" else None,
        detection_count=2 if status == "completed" else 0,
        model_name="yolo11n",
        model_version="yolo11n-test",
        device="cpu",
        started_at=NOW,
        finished_at=NOW if status == "completed" else None,
        dispatch_attempts=1,
    )
    session.add(task)
    await session.flush()

    result = VisionResult(
        tenant_id=tenant_id,
        task_id=task.id,
        task_uid=task_uid,
        artifact_path=f"jobs/{task_uid}/result.json",
        artifact_bytes=1024,
        artifact_file_sha256=uuid.uuid4().hex * 2,
        result_sha256=uuid.uuid4().hex * 2,
        result_schema_version="vision-result/v1",
        frame_count=2,
        detection_count=2,
        class_counts={"person": 2},
        peak_confidence=Decimal("0.9300"),
        inference_ms=23,
        event_count=event_count if with_events else 0,
        summary={
            "detection_count": 2,
            "class_counts": {"person": 2},
            "peak_confidence": 0.93,
            "inference_ms": 23,
            "processed_frames": 2,
        },
        trigger_status=trigger_status,
        trigger_error=trigger_error,
    )
    session.add(result)

    events: list[VisionEvent] = []
    if with_events:
        for index in range(event_count):
            row = VisionEvent(
                tenant_id=tenant_id,
                node_id=None,
                device_id=None,
                event_type="vision.detection",
                severity="medium",
                occurred_at=NOW - timedelta(seconds=30 - index),
                confidence=Decimal("0.9100"),
                bbox={"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0},
                model_version="yolo11n-test",
                payload={
                    "source": "video_upload",
                    "task_uid": task_uid,
                    "frame_id": index,
                    "timestamp_ms": index * 40,
                },
                dedup_key=f"vision:{task_uid}:{index}",
                status="new",
            )
            session.add(row)
            events.append(row)

    await session.commit()
    return task, result, events


async def _runs_for(session, task_uid: str, *, tenant_id=TENANT) -> list[WorkflowRun]:
    rows = await session.scalars(
        select(WorkflowRun).where(
            WorkflowRun.tenant_id == tenant_id,
            WorkflowRun.trigger_type == "event",
            WorkflowRun.trigger_payload["vision_task_uid"].astext == task_uid,
        )
    )
    return list(rows.all())


async def _published_version_id(session, workflow_id: int) -> int:
    value = await session.scalar(
        select(WorkflowVersion.id)
        .where(
            WorkflowVersion.tenant_id == TENANT,
            WorkflowVersion.workflow_id == workflow_id,
            WorkflowVersion.version >= 1,
        )
        .order_by(WorkflowVersion.version.desc())
        .limit(1)
    )
    assert value is not None
    return int(value)


async def _refresh_result(session, result: VisionResult) -> VisionResult:
    await session.refresh(result)
    return result


# -------------------------------------------------------------------- fixtures


@pytest.fixture
def tenant_context():
    token = set_tenant_id(TENANT)
    yield TENANT
    reset_tenant_id(token)


@pytest.fixture
async def published_workflow(client, admin_token) -> int:
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


@pytest.fixture
async def draft_workflow(client, admin_token) -> int:
    name = get_settings().vision_trigger_workflow_name
    created = await client.post(
        "/api/v1/workflows",
        json={"name": name, "dsl": MINIMAL_DSL},
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 200, created.text
    return created.json()["data"]["id"]


@pytest.fixture
async def completed_task_with_events(session, tenant_context):
    return await _seed_task(session)


@pytest.fixture
async def running_task_with_events(session, tenant_context):
    return await _seed_task(session, status="running")


@pytest.fixture
async def completed_task_without_events(session, tenant_context):
    return await _seed_task(session, event_count=0, with_events=False)


# ------------------------------------------------------------------ repo lookup


async def test_find_published_by_name_returns_only_published(
    session, tenant_context, published_workflow
):
    repo = WorkflowRepo(session)
    name = get_settings().vision_trigger_workflow_name

    found = await repo.find_published_by_name(TENANT, name)
    assert found is not None
    assert found.id == published_workflow
    assert found.status == "published"
    assert found.current_version >= 1

    assert await repo.find_published_by_name(TENANT, "不存在的视觉工作流") is None
    assert await repo.find_published_by_name(OTHER_TENANT, name) is None


# ------------------------------------------------------------- trigger success


async def test_trigger_creates_one_event_run_with_links(
    session, published_workflow, completed_task_with_events
):
    task, result, events = completed_task_with_events
    event_ids = [row.id for row in events]

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "triggered"
    assert outcome.workflow_run_id is not None
    assert outcome.error is None

    runs = await _runs_for(session, task.task_uid)
    assert len(runs) == 1
    run = runs[0]
    assert run.id == outcome.workflow_run_id
    assert run.trigger_type == "event"
    assert run.trigger_payload["source"] == "vision"
    assert run.trigger_payload["vision_task_id"] == task.id
    assert run.trigger_payload["vision_task_uid"] == task.task_uid
    assert run.trigger_payload["vision_event_ids"] == event_ids
    assert run.trigger_payload["first_event_id"] == event_ids[0]
    assert run.input["task_uid"] == task.task_uid
    assert run.input["video_filename"] == task.video_filename
    assert run.input["event_ids"] == event_ids
    assert run.input["event_count"] == len(event_ids)
    assert run.input["summary"] == result.summary

    await _refresh_result(session, result)
    assert result.trigger_status == "triggered"
    assert result.workflow_run_id == run.id
    assert result.triggered_at is not None
    assert result.trigger_error is None

    await session.refresh(task)
    assert task.status == "completed"


async def test_replay_poll_upload_restart_paths_do_not_create_second_run(
    session, published_workflow, completed_task_with_events
):
    task, result, _ = completed_task_with_events
    service = VisionTriggerService(session)

    first = await service.trigger(task, result)
    # 轮询器重复收到 completed。
    second = await service.trigger(task, result)
    # 重复上传命中既有任务（Task 11 dedup）后再次 ingest。
    third = await service.trigger(task, result)
    # 重启后补派发/再次对账。
    fourth = await service.trigger(task, result)

    for outcome in (first, second, third, fourth):
        assert outcome.status == "triggered"
        assert outcome.workflow_run_id == first.workflow_run_id

    assert len(await _runs_for(session, task.task_uid)) == 1
    await _refresh_result(session, result)
    assert result.trigger_status == "triggered"
    assert result.workflow_run_id == first.workflow_run_id


async def test_existing_event_run_is_reused_and_returned(
    session, published_workflow, completed_task_with_events
):
    task, result, events = completed_task_with_events
    version_id = await _published_version_id(session, published_workflow)
    existing = WorkflowRun(
        tenant_id=TENANT,
        workflow_version_id=version_id,
        trigger_type="event",
        trigger_payload={
            "source": "vision",
            "vision_task_uid": task.task_uid,
            "vision_event_ids": [row.id for row in events],
        },
        status="succeeded",
        input={"task_uid": task.task_uid},
    )
    session.add(existing)
    await session.commit()

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "triggered"
    assert outcome.workflow_run_id == existing.id
    assert outcome.error is None
    assert len(await _runs_for(session, task.task_uid)) == 1

    await _refresh_result(session, result)
    assert result.trigger_status == "triggered"
    assert result.workflow_run_id == existing.id


async def test_success_clears_previous_trigger_error(
    session, published_workflow, completed_task_with_events
):
    task, result, _ = completed_task_with_events
    result.trigger_status = "skipped"
    result.trigger_error = "上次未找到已发布工作流"
    await session.commit()

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "triggered"
    await _refresh_result(session, result)
    assert result.trigger_status == "triggered"
    assert result.trigger_error is None


# --------------------------------------------------------------- skip branches


async def test_missing_workflow_is_skipped_and_task_stays_completed(
    session, tenant_context, completed_task_with_events
):
    task, result, _ = completed_task_with_events

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "skipped"
    assert outcome.workflow_run_id is None
    assert outcome.error
    assert await _runs_for(session, task.task_uid) == []

    await _refresh_result(session, result)
    assert result.trigger_status == "skipped"
    assert result.workflow_run_id is None
    assert result.trigger_error

    await session.refresh(task)
    assert task.status == "completed"


async def test_draft_workflow_is_not_used_and_marks_skipped(
    session, tenant_context, draft_workflow, completed_task_with_events
):
    task, result, _ = completed_task_with_events
    name = get_settings().vision_trigger_workflow_name
    assert await WorkflowRepo(session).find_published_by_name(TENANT, name) is None

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "skipped"
    assert outcome.workflow_run_id is None
    assert await _runs_for(session, task.task_uid) == []

    await _refresh_result(session, result)
    assert result.trigger_status == "skipped"
    assert result.workflow_run_id is None
    await session.refresh(task)
    assert task.status == "completed"


async def test_running_task_is_not_triggered(
    session, published_workflow, running_task_with_events
):
    task, result, _ = running_task_with_events

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "skipped"
    assert await _runs_for(session, task.task_uid) == []

    await _refresh_result(session, result)
    assert result.trigger_status == "skipped"
    await session.refresh(task)
    assert task.status == "running"


async def test_completed_task_without_events_is_not_triggered(
    session, published_workflow, completed_task_without_events
):
    task, result, _ = completed_task_without_events

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "skipped"
    assert await _runs_for(session, task.task_uid) == []

    await _refresh_result(session, result)
    assert result.trigger_status == "skipped"
    await session.refresh(task)
    assert task.status == "completed"


# -------------------------------------------------------------- failure branch


async def test_run_creation_failure_is_recorded_without_task_regression(
    session, published_workflow, completed_task_with_events
):
    task, result, _ = completed_task_with_events
    version_id = await _published_version_id(session, published_workflow)
    # 模拟"发布版本缺失"导致 RunService.create 抛 Conflict。
    await session.execute(delete(WorkflowVersion).where(WorkflowVersion.id == version_id))
    await session.commit()

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "failed"
    assert outcome.workflow_run_id is None
    assert outcome.error

    assert await _runs_for(session, task.task_uid) == []
    await _refresh_result(session, result)
    assert result.trigger_status == "failed"
    assert result.trigger_error
    assert result.workflow_run_id is None

    await session.refresh(task)
    assert task.status == "completed"
    assert task.error_code is None


# --------------------------------------------------------- unique index guard


async def test_unique_index_blocks_second_event_run_for_same_vision_task(
    session, published_workflow, completed_task_with_events
):
    task, _, _ = completed_task_with_events
    version_id = await _published_version_id(session, published_workflow)
    first = WorkflowRun(
        tenant_id=TENANT,
        workflow_version_id=version_id,
        trigger_type="event",
        trigger_payload={"source": "vision", "vision_task_uid": task.task_uid},
        status="pending",
    )
    session.add(first)
    await session.commit()

    session.add(
        WorkflowRun(
            tenant_id=TENANT,
            workflow_version_id=version_id,
            trigger_type="event",
            trigger_payload={"source": "vision", "vision_task_uid": task.task_uid},
            status="pending",
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()
    # 回滚会过期 session 内全部实例；async 下直接读属性会触发懒加载，
    # 先显式 refresh 再断言，避免 MissingGreenlet。
    await session.refresh(task)

    count = await session.scalar(
        select(func.count())
        .select_from(WorkflowRun)
        .where(
            WorkflowRun.tenant_id == TENANT,
            WorkflowRun.trigger_type == "event",
            WorkflowRun.trigger_payload["vision_task_uid"].astext == task.task_uid,
        )
    )
    assert count == 1


# ------------------------------------------------ 开关与 SAVEPOINT 竞争兜底


async def test_trigger_disabled_is_skipped_and_creates_no_run(
    session, published_workflow, completed_task_with_events, monkeypatch
):
    task, result, _ = completed_task_with_events
    monkeypatch.setattr(get_settings(), "vision_trigger_enabled", False)

    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "skipped"
    assert outcome.workflow_run_id is None
    assert outcome.error
    assert await _runs_for(session, task.task_uid) == []

    await _refresh_result(session, result)
    assert result.trigger_status == "skipped"
    assert result.workflow_run_id is None
    assert result.trigger_error

    await session.refresh(task)
    assert task.status == "completed"


async def test_integrity_conflict_uses_savepoint_and_keeps_caller_writes(
    session, published_workflow, completed_task_with_events, monkeypatch
):
    task, result, _ = completed_task_with_events
    service = VisionTriggerService(session)

    first = await service.trigger(task, result)
    assert first.status == "triggered"

    # 调用方在同一事务里的未提交对象：SAVEPOINT 回滚不得把它一并撤销。
    marker = VisionEvent(
        tenant_id=TENANT,
        event_type="vision.detection",
        severity="low",
        occurred_at=NOW,
        confidence=Decimal("0.5000"),
        payload={"source": "test"},
        dedup_key=f"pending-{uuid.uuid4().hex}",
        status="new",
    )
    session.add(marker)
    await session.flush()

    original = VisionRepo.find_run_by_vision_task
    calls = {"count": 0}

    async def miss_once(self, tenant_id, task_uid):
        calls["count"] += 1
        if calls["count"] == 1:
            # 强制跳过应用层幂等查询，让插入真正撞上部分唯一索引。
            return None
        return await original(self, tenant_id, task_uid)

    monkeypatch.setattr(VisionRepo, "find_run_by_vision_task", miss_once)

    outcome = await service.trigger(task, result)

    assert outcome.status == "triggered"
    assert outcome.workflow_run_id == first.workflow_run_id
    assert outcome.error is None
    assert calls["count"] >= 2, "应经过预查 miss 与冲突后回查"
    assert len(await _runs_for(session, task.task_uid)) == 1

    # 只有本次插入被 SAVEPOINT 回滚；调用方未提交对象仍可读且行仍在。
    assert marker in session
    assert marker.dedup_key
    assert (
        await session.scalar(
            select(func.count())
            .select_from(VisionEvent)
            .where(VisionEvent.id == marker.id)
        )
        == 1
    )

    await _refresh_result(session, result)
    assert result.trigger_status == "triggered"
    assert result.workflow_run_id == first.workflow_run_id

