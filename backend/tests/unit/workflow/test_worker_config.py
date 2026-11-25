"""Celery 配置单元测试：只读配置，不连接 broker。"""

from __future__ import annotations

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
