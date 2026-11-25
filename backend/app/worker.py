"""Celery 应用与 worker 进程级事件循环。

关键约定（Windows + asyncpg）：

- asyncpg 连接池绑定创建它的事件循环，不能每个任务 ``asyncio.run`` 一次；
- 因此每个 worker 进程只启动一个常驻后台事件循环（daemon 线程），所有
  数据库 / 观测协程都通过 :func:`run_in_worker_loop` 提交到该循环；
- worker 启动失败（观测、stale 扫描、broker）只记录日志，绝不让 worker
  进程起不来；业务入队失败也只返回 ``False``。

本模块只负责进程生命周期与 Celery 配置，任务实现见
``app.tasks.workflow_tasks``。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Coroutine

from celery import Celery
from celery.signals import (
    worker_process_init,
    worker_process_shutdown,
    worker_ready,
)

from app.core.config import get_settings
from app.core.tenancy import (
    current_tenant_id_or_none,
    reset_tenant_id,
    set_tenant_id,
)
from app.observability import context as obs_context

logger = logging.getLogger(__name__)

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_loop_lock = threading.Lock()
# 观测写入器只在进程内装配一次；主进程与每个 prefork 子进程各自持有。
_obs_initialized = False


def _build_celery_app() -> Celery:
    settings = get_settings()
    broker = (settings.celery_broker_url or "").strip() or settings.rabbitmq_url
    backend = (settings.celery_result_backend or "").strip() or None

    # 显式声明任务模块：Celery 默认 autodiscover 只找 ``app.tasks.tasks``，
    # 不会加载 ``app.tasks.workflow_tasks``，导致 worker 把消息当作未注册任务丢弃。
    app = Celery(
        "edgepilot",
        broker=broker,
        backend=backend,
        include=["app.tasks.workflow_tasks"],
    )
    app.conf.update(
        # 结果不落库：运行状态以 workflow_runs 为准，避免重复存储。
        task_ignore_result=True,
        task_acks_late=True,
        worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True,
        timezone="UTC",
        enable_utc=True,
        task_routes={
            "workflow.*": {"queue": "q.workflow"},
            "ingest.*": {"queue": "q.ingest"},
            "notify.*": {"queue": "q.notify"},
        },
        beat_schedule={
            "workflow-scan-approval-timeouts": {
                "task": "workflow.scan_approval_timeouts",
                "schedule": float(settings.workflow_scan_interval_s),
            },
            "workflow-requeue-stale-runs": {
                "task": "workflow.requeue_stale_runs",
                "schedule": 30.0,
            },
        },
    )
    return app


celery_app = _build_celery_app()


# ---------------------------------------------------------------- 常驻事件循环


def start_worker_loop() -> asyncio.AbstractEventLoop:
    """启动（或复用）当前进程唯一的后台事件循环并返回它。"""
    global _loop, _loop_thread

    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop

        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _runner() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                # 进程退出路径；仅在循环主动 stop 时走到这里。
                try:
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:  # noqa: BLE001 - 停机清理失败不影响进程退出
                    logger.debug("worker 事件循环清理失败", exc_info=True)
                finally:
                    loop.close()

        thread = threading.Thread(
            target=_runner, name="edgepilot-worker-loop", daemon=True
        )
        _loop = loop
        _loop_thread = thread
        thread.start()
        # 只等待事件循环 ready，启动失败由线程自身的异常日志兜底。
        ready.wait(timeout=5)
        return loop


def run_in_worker_loop(
    factory: Callable[[], Coroutine[Any, Any, Any]],
    timeout: float | None = None,
) -> Any:
    """把协程工厂提交到常驻循环并同步等待结果。

    调用线程的 ``contextvars``（trace / span / sequence / tenant）会先被
    快照成普通值，再在运行于常驻循环的协程内设置、finally 中恢复。这样
    Celery task 恢复的 trace 能进入 ``_execute_run``，同时不把 ``Context``
    对象跨线程/并发任务共享。

    Args:
        factory: 无参可调用对象，返回协程。协程对象在调用线程创建，
            协程体则在常驻循环的 Task 中执行。
        timeout: 等待秒数；超时抛 ``concurrent.futures.TimeoutError``。
    """
    loop = start_worker_loop()

    # 只在调用线程读取值；Context 对象本身不跨任务传递，避免并发进入。
    trace = obs_context.current_trace.get()
    span = obs_context.current_span.get()
    sequence = obs_context.sequence.get()
    tenant = current_tenant_id_or_none()

    coro = factory()

    async def _run_with_caller_context() -> Any:
        tokens: list[tuple[Any, Any]] = []
        tenant_token = None
        try:
            tokens.append(
                (obs_context.current_trace, obs_context.current_trace.set(trace))
            )
            tokens.append(
                (obs_context.current_span, obs_context.current_span.set(span))
            )
            tokens.append(
                (obs_context.sequence, obs_context.sequence.set(sequence))
            )
            if tenant is not None:
                tenant_token = set_tenant_id(tenant)
            return await coro
        finally:
            # 本协程运行在独立 Task 的 context 副本里，reset 不会影响其他任务。
            for var, token in reversed(tokens):
                try:
                    var.reset(token)
                except (ValueError, RuntimeError):
                    logger.debug("worker loop context reset 失败", exc_info=True)
            if tenant_token is not None:
                try:
                    reset_tenant_id(tenant_token)
                except (ValueError, RuntimeError):
                    logger.debug("worker loop tenant reset 失败", exc_info=True)

    future = asyncio.run_coroutine_threadsafe(_run_with_caller_context(), loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError:
        future.cancel()
        raise


# ------------------------------------------------------------------- 生命周期


async def _init_observability() -> None:
    """为当前进程装配观测写入器并启动采集协程。

    prefork 模式下任务在子进程执行，而 ``worker_ready`` 只在主进程触发；
    子进程若不单独初始化，tracer 会拿到 ``NullWriter``，记录只入队不落库。
    """
    global _obs_initialized
    if _obs_initialized:
        return

    settings = get_settings()
    if not settings.obs_enabled:
        _obs_initialized = True
        return

    from app.db import get_session_factory
    from app.observability import get_tracer, set_writer
    from app.observability.live import LiveLayer
    from app.observability.writer import BatchWriter

    set_writer(
        BatchWriter(
            get_session_factory(),
            live=LiveLayer(settings.redis_url, settings.obs_live_ttl_seconds),
        )
    )
    await get_tracer().collector.start()
    _obs_initialized = True


async def _on_worker_ready() -> None:
    """worker_ready 的一次性异步初始化；任何步骤失败都只记日志。"""
    try:
        await _init_observability()
    except Exception:  # noqa: BLE001 - 观测初始化不得阻断 worker 启动
        logger.exception("worker 观测初始化失败，继续启动")

    try:
        from app.tasks.workflow_tasks import enqueue_stale_runs

        await enqueue_stale_runs()
    except Exception:  # noqa: BLE001 - 扫描失败由下一轮 beat 重试
        logger.exception("worker 启动 stale 运行扫描失败")


async def _dispose_engine() -> None:
    from app.db import get_engine

    await get_engine().dispose()


@worker_process_init.connect
def _init_worker_process(**_kwargs: Any) -> None:
    try:
        start_worker_loop()
    except Exception:  # noqa: BLE001 - 循环启动失败让 Celery 自己报错
        logger.exception("worker 进程事件循环启动失败")
        return

    try:
        run_in_worker_loop(_init_observability, timeout=30)
    except Exception:  # noqa: BLE001 - 观测初始化失败不影响消费任务
        logger.exception("worker 子进程观测初始化失败，继续执行任务")


@worker_ready.connect
def _on_worker_ready_signal(**_kwargs: Any) -> None:
    try:
        run_in_worker_loop(_on_worker_ready, timeout=30)
    except Exception:  # noqa: BLE001 - 初始化失败不影响消费任务
        logger.exception("worker ready 初始化失败")


@worker_process_shutdown.connect
def _on_worker_process_shutdown(**_kwargs: Any) -> None:
    try:
        run_in_worker_loop(_dispose_engine, timeout=10)
    except Exception:  # noqa: BLE001 - 关闭连接池失败不阻塞退出
        logger.exception("worker 进程关闭连接池失败")


__all__ = [
    "celery_app",
    "run_in_worker_loop",
    "start_worker_loop",
]
