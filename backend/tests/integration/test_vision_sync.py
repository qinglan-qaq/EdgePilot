"""Task 14 集成测试：轮询间隔、指数退避、404/不可达/超时、启动补派发与自动 ingest。

全部使用真实数据库事务（conftest 的 session_factory 绑定同一连接 + 外层事务），
外部推理服务用 ``FakeVisionClient`` 替身，不访问任何真实网络。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import Settings, get_settings
from app.models.vision import VisionTask
from app.services.vision_client import VisionJobNotFound, VisionServiceError
from app.services.vision_sync import VisionSyncPoller
from tests.conftest import TENANT

COMPLETED_STATE = {
    "status": "completed",
    "progress": 100,
    "attempt": 1,
    "result_available": True,
    "result_bytes": 512,
    "result_sha256": "b" * 64,
    "result_file_sha256": "c" * 64,
    "result_schema_version": "vision-result/v1",
    "processed_frames": 3,
    "detection_count": 2,
    "video": {"width": 200, "height": 200, "fps": 25.0},
    "model": {"model_name": "yolo11n", "model_version": "yolo11n-abcdef123456"},
    "summary": {"processed_frames": 3, "detection_count": 2, "class_counts": {"person": 2}},
    "filename": "gate.mp4",
}


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        vision_service_url="http://vision.test",
        vision_poll_interval_s=2,
        vision_poll_backoff_max_s=30,
        vision_poll_max_failures=30,
        vision_task_timeout_s=1800,
    )


class FakeVisionClient:
    """只实现轮询器需要的协议方法；调用计数供断言。"""

    def __init__(self) -> None:
        self.get_calls = 0
        self.create_calls: list[str] = []
        self.cancel_calls = 0
        self.fail = False
        self.not_found = False
        self.remote_status = "running"
        self.progress = 40
        self.completed_state = dict(COMPLETED_STATE)
        self.failed_state = {
            "error_code": "decode_failed",
            "error_message": "视频解码失败",
        }

    async def get_job(self, task_uid: str) -> dict:
        self.get_calls += 1
        if self.fail:
            raise VisionServiceError("推理服务不可达: ConnectError")
        if self.not_found:
            raise VisionJobNotFound("推理服务任务不存在", status=404)
        if self.remote_status == "completed":
            return dict(self.completed_state, task_uid=task_uid)
        if self.remote_status == "failed":
            return {
                "task_uid": task_uid,
                "status": "failed",
                "progress": 0,
                "attempt": 1,
                "result_available": False,
                **self.failed_state,
            }
        if self.remote_status == "cancelled":
            return {
                "task_uid": task_uid,
                "status": "cancelled",
                "progress": 0,
                "attempt": 1,
                "result_available": False,
            }
        return {
            "task_uid": task_uid,
            "status": "running",
            "progress": self.progress,
            "attempt": 1,
            "result_available": False,
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
        self.create_calls.append(task_uid)
        return {"task_uid": task_uid, "status": "pending", "progress": 0, "attempt": 1}

    async def cancel_job(self, task_uid: str) -> dict:
        self.cancel_calls += 1
        return {"task_uid": task_uid, "status": "cancelled", "progress": 0}

    async def results(self, task_uid: str) -> dict:
        return dict(self.completed_state, task_uid=task_uid)


@pytest.fixture
def fake_client() -> FakeVisionClient:
    return FakeVisionClient()


@pytest.fixture
def settings() -> Settings:
    return _settings()


@pytest.fixture
def poller(session_factory, fake_client, settings) -> VisionSyncPoller:
    return VisionSyncPoller(session_factory, fake_client, settings)


async def _seed_task(
    session,
    *,
    status: str = "running",
    created_at: datetime | None = None,
    last_polled_at: datetime | None = None,
    dispatch_attempts: int = 0,
    task_uid: str | None = None,
) -> VisionTask:
    uid = task_uid or f"task-{uuid.uuid4().hex[:8]}"
    task = VisionTask(
        tenant_id=TENANT,
        task_uid=uid,
        dedup_key=f"dedup-{uid}",
        status=status,
        source_type="upload",
        video_filename="gate.mp4",
        video_path=f"videos/{TENANT}/{uid}.mp4",
        video_sha256="a" * 64,
        file_size_bytes=64,
        conf_threshold=Decimal("0.250"),
        frame_stride=1,
        target_samples=900,
        progress=0,
        detection_count=0,
        dispatch_attempts=dispatch_attempts,
        poll_failures=0,
        poll_misses=0,
        created_at=created_at or datetime.now(timezone.utc),
        last_polled_at=last_polled_at,
    )
    session.add(task)
    await session.flush()
    return task


async def _rewind_last_polled(session, task: VisionTask, seconds: float = 60.0) -> None:
    """把上次轮询时间拨回过去，避免测试真的 sleep 等待退避。"""
    await session.refresh(task)
    task.last_polled_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    await session.flush()


# ---------------------------------------------------------------- 基础轮询


async def test_terminal_tasks_are_not_polled(poller, fake_client, session):
    await _seed_task(session, status="completed")
    await _seed_task(session, status="failed")
    await _seed_task(session, status="cancelled")

    await poller.tick()

    assert fake_client.get_calls == 0


async def test_tick_polls_due_task_then_respects_two_second_interval(
    poller, fake_client, session
):
    task = await _seed_task(session, status="running")

    await poller.tick()
    assert fake_client.get_calls == 1
    await session.refresh(task)
    assert task.last_polled_at is not None
    assert task.status == "running"

    # 2s 内再次 tick 不得重复请求远端。
    await poller.tick()
    assert fake_client.get_calls == 1

    task.last_polled_at = datetime.now(timezone.utc) - timedelta(seconds=2.5)
    await session.flush()
    await poller.tick()
    assert fake_client.get_calls == 2


async def test_delay_for_formula_grows_and_caps(poller, fake_client, session):
    task = await _seed_task(session, status="running")

    task.poll_failures = 0
    assert poller.delay_for(task) == 2
    task.poll_failures = 1
    assert poller.delay_for(task) == 4
    task.poll_failures = 2
    assert poller.delay_for(task) == 8
    task.poll_failures = 10
    assert poller.delay_for(task) == 30


async def test_network_error_increments_failures_and_keeps_task_running(
    poller, fake_client, session
):
    task = await _seed_task(session, status="running")
    fake_client.fail = True

    await poller.tick()
    await session.refresh(task)

    assert task.status == "running"
    assert task.poll_failures == 1
    assert task.error_code is None
    assert task.last_polled_at is not None


async def test_backoff_grows_across_failed_ticks_and_resets_on_success(
    poller, fake_client, session
):
    task = await _seed_task(session, status="running")
    fake_client.fail = True

    await poller.tick()
    await _rewind_last_polled(session, task)
    await poller.tick()
    await session.refresh(task)
    assert task.poll_failures == 2
    assert poller.delay_for(task) == 8

    fake_client.fail = False
    await _rewind_last_polled(session, task)
    await poller.tick()
    await session.refresh(task)
    assert task.poll_failures == 0


async def test_thirty_consecutive_failures_mark_service_unavailable(
    poller, fake_client, session
):
    task = await _seed_task(session, status="running")
    fake_client.fail = True

    for _ in range(30):
        await poller.tick()
        await _rewind_last_polled(session, task)

    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "vision_service_unavailable"
    assert task.finished_at is not None

    calls = fake_client.get_calls
    await poller.tick()
    assert fake_client.get_calls == calls  # 终态不再轮询


async def test_three_consecutive_404_marks_job_lost(poller, fake_client, session):
    task = await _seed_task(session, status="running")
    fake_client.not_found = True

    for _ in range(2):
        await poller.tick()
        await _rewind_last_polled(session, task)
    await session.refresh(task)
    assert task.status == "running"

    await poller.tick()
    await session.refresh(task)
    assert task.status == "failed"
    assert task.error_code == "vision_job_lost"
    assert task.finished_at is not None

    calls = fake_client.get_calls
    await poller.tick()
    assert fake_client.get_calls == calls


# ---------------------------------------------------------------- 超时与终态


async def test_timeout_cancels_remote_and_fails_task(poller, fake_client, session):
    task = await _seed_task(
        session,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=1801),
    )

    await poller.tick()
    await session.refresh(task)

    assert fake_client.cancel_calls == 1
    assert task.status == "failed"
    assert task.error_code == "timeout"
    assert task.finished_at is not None


async def test_timeout_window_starts_at_latest_dispatch(poller, fake_client, session):
    """重试后的任务按最近一次派发计时，不能因旧 created_at 立刻超时。"""
    task = await _seed_task(
        session,
        status="running",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=7200),
    )
    task.dispatched_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await session.flush()

    await poller.tick()
    await session.refresh(task)

    assert fake_client.cancel_calls == 0
    assert task.status == "running"
    assert task.error_code is None


async def test_remote_failed_copies_error_and_stops_polling(poller, fake_client, session):
    task = await _seed_task(session, status="running")
    fake_client.remote_status = "failed"

    await poller.tick()
    await session.refresh(task)

    assert task.status == "failed"
    assert task.error_code == "decode_failed"
    assert task.error_message == "视频解码失败"
    assert task.finished_at is not None

    calls = fake_client.get_calls
    await poller.tick()
    assert fake_client.get_calls == calls


async def test_completed_job_triggers_result_ingest(poller, fake_client, session, monkeypatch):
    from app.services.vision_result_service import VisionResultService

    calls: list[tuple[str, dict]] = []

    async def fake_ingest(self, task, remote_state):
        calls.append((task.task_uid, remote_state))
        return None

    monkeypatch.setattr(VisionResultService, "ingest", fake_ingest)
    task = await _seed_task(session, status="running")
    fake_client.remote_status = "completed"

    await poller.tick()

    assert len(calls) == 1
    task_uid, remote_state = calls[0]
    assert task_uid == task.task_uid
    assert remote_state["status"] == "completed"
    assert remote_state["result_sha256"] == "b" * 64
    assert remote_state["result_file_sha256"] == "c" * 64
    assert remote_state["result_schema_version"] == "vision-result/v1"


# ---------------------------------------------------------------- 启动恢复


async def test_start_redispatches_pending_never_dispatched(
    poller, fake_client, session
):
    task = await _seed_task(
        session, status="pending", dispatch_attempts=0, task_uid="task-redispatch"
    )
    already_dispatched = await _seed_task(
        session, status="running", dispatch_attempts=1, task_uid="task-already-running"
    )

    video = get_settings().vision_storage_root.resolve() / task.video_path
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16)
    try:
        await poller.start()
        try:
            deadline = time.monotonic() + 3.0
            while not fake_client.create_calls and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        finally:
            await poller.stop()
    finally:
        video.unlink(missing_ok=True)

    assert fake_client.create_calls == [task.task_uid]
    assert already_dispatched.task_uid not in fake_client.create_calls
    await session.refresh(task)
    assert task.dispatch_attempts >= 1


# ------------------------------------------------------------------ lifespan


def _lifespan_settings(**overrides):
    base = {
        "_env_file": None,
        "mqtt_enabled": False,
        "vision_enabled": True,
        "vision_service_url": "http://vision.test",
    }
    base.update(overrides)
    return Settings(**base)


def test_lifespan_starts_poller_only_when_vision_enabled(monkeypatch):
    """vision_enabled=true 时随 API 启停；false 时不得触碰 vision-service。"""
    from fastapi.testclient import TestClient

    import app.core.config as config_module
    import app.main as main_module
    import app.services.vision_sync as sync_module

    events: list[str] = []

    class FakePoller:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("init")

        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(sync_module, "VisionSyncPoller", FakePoller)
    monkeypatch.setattr(main_module, "VisionSyncPoller", FakePoller, raising=False)
    monkeypatch.setattr(config_module, "get_settings", lambda: _lifespan_settings())

    app = main_module.create_app()
    with TestClient(app):
        assert events == ["init", "start"]
    assert events[-1] == "stop"


def test_lifespan_skips_poller_when_vision_disabled(monkeypatch):
    from fastapi.testclient import TestClient

    import app.core.config as config_module
    import app.main as main_module
    import app.services.vision_sync as sync_module

    events: list[str] = []

    class FakePoller:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("init")

        async def start(self) -> None:
            events.append("start")

        async def stop(self) -> None:
            events.append("stop")

    monkeypatch.setattr(sync_module, "VisionSyncPoller", FakePoller)
    monkeypatch.setattr(main_module, "VisionSyncPoller", FakePoller, raising=False)
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: _lifespan_settings(vision_enabled=False),
    )

    app = main_module.create_app()
    with TestClient(app):
        assert events == []
    assert events == []
