"""Celery 配置单元测试：只读配置，不连接 broker。"""

from __future__ import annotations

from types import SimpleNamespace

from app import worker as worker_module
from app.core.config import get_settings
from app.tasks import workflow_tasks
from app.worker import celery_app


def test_worker_routes_queues_and_beat_schedule():
    settings = get_settings()
    conf = celery_app.conf

    assert celery_app.main == "edgepilot"
    assert conf.task_routes["workflow.*"]["queue"] == "q.workflow"
    assert conf.task_routes["ingest.*"]["queue"] == "q.ingest"
    assert conf.task_routes["notify.*"]["queue"] == "q.notify"

    assert conf.task_ignore_result is True
    assert conf.task_acks_late is True
    assert conf.worker_prefetch_multiplier == 1
    assert conf.broker_connection_retry_on_startup is True
    assert conf.timezone == "UTC"
    assert conf.enable_utc is True

    beat = conf.beat_schedule
    assert beat["workflow-scan-approval-timeouts"]["task"] == (
        "workflow.scan_approval_timeouts"
    )
    assert beat["workflow-scan-approval-timeouts"]["schedule"] == float(
        settings.workflow_scan_interval_s
    )
    assert beat["workflow-requeue-stale-runs"]["task"] == (
        "workflow.requeue_stale_runs"
    )
    assert beat["workflow-requeue-stale-runs"]["schedule"] == 30.0


def test_worker_imports_task_modules_on_startup():
    """worker 启动必须导入任务模块；否则消息会被判定为未注册任务并丢弃。"""
    assert "app.tasks.workflow_tasks" in celery_app.conf.include


def test_prefork_child_initializes_observability(monkeypatch):
    """prefork 子进程执行任务，必须自行装配观测写入器。

    主进程的 ``worker_ready`` 不会在子进程触发；漏掉这一步时 tracer 使用
    ``NullWriter``，trace/span 记录只入队不落库。
    """
    calls: list = []
    monkeypatch.setattr(worker_module, "start_worker_loop", lambda: object())
    monkeypatch.setattr(
        worker_module,
        "run_in_worker_loop",
        lambda factory, timeout=None: calls.append(factory),
    )

    worker_module._init_worker_process()

    assert worker_module._init_observability in calls


async def test_observability_init_is_idempotent(monkeypatch):
    """同一进程内重复初始化不得重复启动采集协程。"""
    started: list[int] = []
    writers: list[object] = []

    class FakeCollector:
        async def start(self):
            started.append(1)

    from app import db as db_module
    from app import observability as obs_module
    from app.observability import live as live_module
    from app.observability import writer as writer_module

    monkeypatch.setattr(worker_module, "_obs_initialized", False)
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(
            obs_enabled=True,
            redis_url="redis://localhost:6379/0",
            obs_live_ttl_seconds=60,
        ),
    )
    monkeypatch.setattr(
        db_module, "get_session_factory", lambda: object(), raising=False
    )
    monkeypatch.setattr(
        obs_module, "get_tracer", lambda: SimpleNamespace(collector=FakeCollector())
    )
    monkeypatch.setattr(obs_module, "set_writer", writers.append)
    monkeypatch.setattr(writer_module, "BatchWriter", lambda *a, **k: object())
    monkeypatch.setattr(live_module, "LiveLayer", lambda *a, **k: object())

    await worker_module._init_observability()
    await worker_module._init_observability()

    assert started == [1]
    assert len(writers) == 1
    assert worker_module._obs_initialized is True


def test_task_names_are_registered():
    assert workflow_tasks.execute_run.name == "workflow.execute_run"
    assert workflow_tasks.scan_approval_timeouts.name == (
        "workflow.scan_approval_timeouts"
    )
    assert workflow_tasks.requeue_stale_runs.name == (
        "workflow.requeue_stale_runs"
    )


def test_enqueue_is_disabled_without_celery(monkeypatch):
    calls: list[tuple] = []

    def fake_send_task(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    monkeypatch.setattr(get_settings(), "celery_enabled", False)

    assert workflow_tasks.enqueue_workflow_run(12, "review") is False
    assert calls == []


def test_enqueue_failure_is_swallowed(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("broker down")

    monkeypatch.setattr(celery_app, "send_task", boom)
    monkeypatch.setattr(get_settings(), "celery_enabled", True)

    assert workflow_tasks.enqueue_workflow_run(12) is False
