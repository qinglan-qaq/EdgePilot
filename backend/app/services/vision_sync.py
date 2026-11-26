"""Vision 任务轮询器（Task 14）：固定间隔扫描 + 指数退避 + 超时 + 启动恢复。

职责（全部在后台，不参与请求链路）：

* 每 ``vision_poll_interval_s`` 秒扫描 ``status in (pending, running)`` 的任务；
* 单任务失败计数 ``poll_failures``，下次轮询延迟
  ``min(interval * 2**failures, backoff_max)``，成功即清零；
* 连续网络失败达到 ``vision_poll_max_failures`` → ``failed(vision_service_unavailable)``；
  连续 404 达到 3 次 → ``failed(vision_job_lost)``；
* 任务创建超过 ``vision_task_timeout_s`` 仍未终态 → 请求远端取消并落 ``failed(timeout)``；
* 远端 ``completed`` → 交给 ``VisionResultService.ingest``（内部按
  「结果 → 事件 → 完成」提交并触发联动）；``failed/cancelled`` 复制错误码；
* 启动时为 ``pending && dispatch_attempts == 0`` 的任务补派发，失败只记日志。

事务约定：每个任务独立提交；本模块**绝不**调用 ``session.rollback()``
（共享事务的调用方不能被轮询器回滚）。意外异常只记录日志并结束本轮扫描，
下一轮使用新会话继续。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.exceptions import AppError
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.vision import VisionTask
from app.repositories.vision_repo import VisionRepo
from app.services.vision_client import (
    VisionJobNotFound,
    VisionServiceError,
)
from app.services.vision_result_service import VisionResultService
from app.services.vision_storage import VisionStorage

logger = logging.getLogger(__name__)

# 远端状态 → 任务状态。未列出的非终态一律保持原状态。
_RUNNING_REMOTE = frozenset({"running", "processing"})
_TERMINAL_REMOTE = frozenset({"failed", "cancelled"})
_JOB_LOST_MISSES = 3
_ERROR_MESSAGE_LIMIT = 512
_DEFAULT_INTERVAL_S = 2.0


class VisionSyncPoller:
    """vision-service 任务对账轮询器。

    Args:
        session_factory: ``async_sessionmaker``；每轮扫描/每任务各自独立会话。
        client: 具备 ``get_job / create_job / cancel_job`` 的客户端（测试用替身）。
        settings: 轮询间隔、退避上限、超时等配置。
    """

    def __init__(self, session_factory: Any, client: Any, settings: Any) -> None:
        self.session_factory = session_factory
        self.client = client
        self.settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """补派发未派发任务并启动后台循环；任何失败都不抛给调用方。"""
        if self._task is not None:
            return
        try:
            await self._redispatch_pending()
        except Exception:  # noqa: BLE001 - 启动失败不得阻断 API
            logger.exception("vision 轮询器启动补派发失败，交由后续轮询/超时处理")
        try:
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._run(), name="vision-sync-poller")
        except Exception:  # noqa: BLE001 - 创建后台任务失败同样不得阻断 API
            logger.exception("vision 轮询器后台循环启动失败")
            self._stop = None
            self._task = None

    async def stop(self) -> None:
        """取消后台循环并等待其退出；可重复调用。"""
        task = self._task
        self._task = None
        stop = self._stop
        self._stop = None
        if stop is not None:
            stop.set()
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - 停止失败只记录，不影响关停流程
            logger.exception("vision 轮询器停止异常")

    async def _run(self) -> None:
        """周期循环：等待间隔或被要求停止，然后执行一次 ``tick``。"""
        stop = self._stop
        if stop is None:
            return
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval())
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单轮异常不能杀掉轮询循环
                logger.exception("vision 轮询周期异常，等待下一轮")

    def _interval(self) -> float:
        try:
            interval = float(self.settings.vision_poll_interval_s)
        except (TypeError, ValueError):
            return _DEFAULT_INTERVAL_S
        return interval if interval > 0 else _DEFAULT_INTERVAL_S

    # ------------------------------------------------------------------ tick
    async def tick(self) -> None:
        """扫描一次活动任务：超时优先，其次按退避判断是否到期。"""
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            tasks = await VisionRepo(session).list_active()
            for task in tasks:
                task_uid = task.task_uid
                token = set_tenant_id(task.tenant_id)
                try:
                    if self._timed_out(task, now):
                        await self._timeout(session, task)
                        continue
                    if self._not_due(task, now):
                        continue
                    await self._sync_one(session, task, now)
                except Exception:  # noqa: BLE001 - 单任务异常中断本轮，避免污染会话
                    logger.exception("vision 轮询单任务异常 task_uid=%s", task_uid)
                    break
                finally:
                    reset_tenant_id(token)

    def delay_for(self, task: VisionTask) -> int:
        """下次轮询延迟（秒）：``min(interval * 2**failures, backoff_max)``。"""
        failures = int(task.poll_failures or 0)
        if failures < 0:
            failures = 0
        delay = self._interval() * (2**failures)
        return int(min(delay, float(self.settings.vision_poll_backoff_max_s)))

    def _timed_out(self, task: VisionTask, now: datetime) -> bool:
        created = task.created_at
        if created is None:
            return False
        return now - _aware(created) > timedelta(
            seconds=float(self.settings.vision_task_timeout_s)
        )

    def _not_due(self, task: VisionTask, now: datetime) -> bool:
        last = task.last_polled_at
        if last is None:
            return False
        return now < _aware(last) + timedelta(seconds=self.delay_for(task))

    # -------------------------------------------------------------- one task
    async def _sync_one(
        self, session: Any, task: VisionTask, now: datetime
    ) -> None:
        """按远端状态更新单个任务；每次分支独立提交。"""
        task.last_polled_at = now
        try:
            state = await self.client.get_job(task.task_uid)
        except VisionJobNotFound:
            await self._on_miss(session, task)
            return
        except VisionServiceError as exc:
            await self._on_unreachable(session, task, exc)
            return

        status = str(state.get("status") or "").strip().lower()
        if status == "completed":
            task.poll_failures = 0
            task.poll_misses = 0
            await self._ingest(session, task, state)
            return
        if status in _TERMINAL_REMOTE:
            self._finish_remote_terminal(task, state, status, now)
            await session.commit()
            return

        # pending / running / queued：远端仍活跃，退避计数清零。
        task.poll_failures = 0
        task.poll_misses = 0
        task.progress = _int_or_none(state.get("progress")) or 0
        if status in _RUNNING_REMOTE:
            task.status = "running"
            if task.started_at is None:
                task.started_at = now
        task.error_code = None
        task.error_message = None
        await session.commit()

    async def _on_miss(self, session: Any, task: VisionTask) -> None:
        """远端 404：累计 poll_misses，连续 3 次判定任务丢失。"""
        task.poll_misses = int(task.poll_misses or 0) + 1
        if task.poll_misses >= _JOB_LOST_MISSES:
            logger.warning(
                "vision 任务连续 %s 次 404，标记丢失 task_uid=%s",
                task.poll_misses,
                task.task_uid,
            )
            self._mark_failed(task, "vision_job_lost", "推理服务任务不存在")
        await session.commit()

    async def _on_unreachable(
        self, session: Any, task: VisionTask, exc: VisionServiceError
    ) -> None:
        """网络/服务错误：累计 poll_failures，达到上限标记服务不可用。"""
        task.poll_failures = int(task.poll_failures or 0) + 1
        limit = int(self.settings.vision_poll_max_failures)
        if task.poll_failures >= limit:
            logger.warning(
                "vision 连续 %s 次不可达，标记服务不可用 task_uid=%s",
                task.poll_failures,
                task.task_uid,
            )
            self._mark_failed(
                task, "vision_service_unavailable", exc.message or "推理服务不可达"
            )
        await session.commit()

    async def _ingest(
        self, session: Any, task: VisionTask, state: dict[str, Any]
    ) -> None:
        """远端 completed → 结果入库（内部提交并触发联动）。"""
        try:
            await VisionResultService(session).ingest(task, state)
        except AppError as exc:
            # artifact 校验失败时 ingest 已把任务落 failed 并提交，这里只记录。
            logger.warning(
                "vision 结果入库失败 task_uid=%s error=%s",
                task.task_uid,
                type(exc).__name__,
            )

    # ---------------------------------------------------------------- 终态
    async def _timeout(self, session: Any, task: VisionTask) -> None:
        """超时：先请求远端取消（失败只记日志），再落 failed(timeout)。"""
        try:
            await self.client.cancel_job(task.task_uid)
        except VisionServiceError as exc:
            logger.warning(
                "vision 超时取消请求失败 task_uid=%s error=%s",
                task.task_uid,
                type(exc).__name__,
            )
        logger.warning("vision 任务超时未完成 task_uid=%s", task.task_uid)
        self._mark_failed(task, "timeout", "任务超时未完成")
        await session.commit()

    def _finish_remote_terminal(
        self,
        task: VisionTask,
        state: dict[str, Any],
        status: str,
        now: datetime,
    ) -> None:
        """复制远端 ``failed/cancelled`` 的错误码与消息。"""
        default_code = "cancelled" if status == "cancelled" else "vision_failed"
        code = state.get("error_code") or default_code
        message = state.get("error_message") or (
            "推理服务任务已取消" if status == "cancelled" else "推理服务任务失败"
        )
        self._mark_failed(task, str(code), str(message), now=now)

    @staticmethod
    def _mark_failed(
        task: VisionTask,
        code: str,
        message: str | None,
        *,
        now: datetime | None = None,
    ) -> None:
        task.status = "failed"
        task.error_code = code[:64]
        text = (message or code).strip() or code
        task.error_message = text[:_ERROR_MESSAGE_LIMIT]
        task.finished_at = now or datetime.now(timezone.utc)

    # ------------------------------------------------------------ 启动补派发
    async def _redispatch_pending(self) -> None:
        """``pending && dispatch_attempts == 0`` 的任务立即派发。"""
        async with self.session_factory() as session:
            tasks = await VisionRepo(session).pending_never_dispatched()
            for task in tasks:
                task_uid = task.task_uid
                token = set_tenant_id(task.tenant_id)
                try:
                    await self._dispatch(session, task)
                    await session.commit()
                except Exception:  # noqa: BLE001 - 单任务异常不阻断其余补派发
                    logger.exception("vision 启动补派发异常 task_uid=%s", task_uid)
                    break
                finally:
                    reset_tenant_id(token)

    async def _dispatch(self, session: Any, task: VisionTask) -> None:
        """调用 vision-service 创建任务；失败只记录，任务交给轮询/超时处理。"""
        storage = VisionStorage(
            self.settings.vision_storage_root,
            max_upload_mb=self.settings.vision_max_upload_mb,
        )
        try:
            # DB 里是相对存储根的路径，派发前解析为绝对路径并校验仍在根内。
            video_path = str(storage.resolve(task.video_path))
        except (OSError, ValueError) as exc:
            logger.warning(
                "vision 补派发视频路径非法 task_uid=%s error=%s",
                task.task_uid,
                type(exc).__name__,
            )
            return

        task.dispatch_attempts = int(task.dispatch_attempts or 0) + 1
        task.dispatched_at = datetime.now(timezone.utc)
        try:
            await self.client.create_job(
                task.task_uid,
                video_path,
                task.video_filename,
                task.video_sha256,
                float(task.conf_threshold),
                int(task.target_samples),
                int(task.frame_stride),
            )
        except VisionServiceError as exc:
            logger.warning(
                "vision 启动补派发失败 task_uid=%s error=%s",
                task.task_uid,
                type(exc).__name__,
            )
            return
        task.status = "pending"


def _aware(value: datetime) -> datetime:
    """DB 读出的时间戳一律按 UTC 处理，避免 naive/aware 比较报错。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["VisionSyncPoller"]
