"""Vision 结果保留清理（T19）：文件删除 + 汇总保留 + 审计。

契约（spec §7.4）：

* 默认只删视频与 ``jobs/<uid>/`` 目录；``vision_tasks`` 汇总与状态保持不变
  （completed 绝不因清理变成 failed），``vision_results`` 行保留、仅把
  ``artifact_path`` 置空并写入 ``purged_at``；
* ``purge_db=True`` 是显式破坏性操作：删除 ``vision_results`` 行并把任务标为
  ``failed`` / ``error_code="purged"``（不新增状态值）；
* ``dry_run=True`` 只统计，不删除、不改库结构，但仍写审计（deleted_files=0）；
* 每个任务写一条 ``audit_logs``（``action="vision.cleanup"``），
  ``detail`` 含 ``actor/task_uid/deleted_files/dry_run/purge_db``，租户取任务自身。

HTTP 不暴露清理接口：仅由 ``scripts/vision_cleanup.py`` 在运维侧执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.vision import VisionTask
from app.repositories.vision_repo import VisionRepo
from app.services.vision_storage import VisionStorage

logger = logging.getLogger(__name__)

CLEANUP_ACTION = "vision.cleanup"
PURGED_ERROR_CODE = "purged"
PURGED_MESSAGE = "结果已按运维清理策略删除"


@dataclass
class CleanupStats:
    """一次清理运行的汇总；脚本直接打印这些字段。"""

    deleted_files: int = 0
    tasks_processed: int = 0
    dry_run: bool = False
    purge_db: bool = False
    actor: str = ""


class CleanupFailed(RuntimeError):
    """单任务清理失败；携带 task_uid 供运维脚本定位并决定退出码。"""

    def __init__(self, task_uid: str) -> None:
        self.task_uid = task_uid
        super().__init__(f"vision cleanup failed for task {task_uid}")


class VisionCleanupService:
    def __init__(self, session: AsyncSession, storage_root: str) -> None:
        self.session = session
        self.repo = VisionRepo(session)
        self.storage = VisionStorage(storage_root)

    async def run(
        self,
        *,
        days: int,
        dry_run: bool = False,
        purge_db: bool = False,
        actor: str,
    ) -> CleanupStats:
        """清理 ``created_at < now - days`` 的终态任务。

        Args:
            days: 保留天数；负值按 0 处理（只清“过期”的终态任务）。
            dry_run: 只统计并写审计，不删文件、不改结果行。
            purge_db: 额外删除 ``vision_results`` 行并把任务标 ``failed(purged)``。
            actor: 执行人（必填），写入每条审计的 ``detail.actor``。
        """
        actor = (actor or "").strip()
        if not actor:
            raise ValueError("清理审计必须提供 actor")

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max(int(days), 0))
        tasks = await self.repo.list_expired(cutoff)

        stats = CleanupStats(dry_run=dry_run, purge_db=purge_db, actor=actor)
        for task in tasks:
            try:
                deleted = await self._cleanup_task(
                    task, now=now, dry_run=dry_run, purge_db=purge_db, actor=actor
                )
            except Exception as exc:  # noqa: BLE001 - 标记 task_uid 后交给脚本收敛
                logger.error(
                    "vision 清理失败 task_uid=%s error=%s",
                    task.task_uid,
                    type(exc).__name__,
                    exc_info=True,
                )
                raise CleanupFailed(task.task_uid) from exc
            stats.deleted_files += deleted
            stats.tasks_processed += 1
        return stats

    # ---------------------------------------------------------------- helpers
    async def _cleanup_task(
        self,
        task: VisionTask,
        *,
        now: datetime,
        dry_run: bool,
        purge_db: bool,
        actor: str,
    ) -> int:
        """处理单个任务；返回本次真正删除的文件数（dry-run 恒为 0）。"""
        deleted = 0
        if dry_run:
            # 只统计“过期任务数”并写审计；deleted_files 按契约记 0，避免误读为已删。
            self._audit(task, actor=actor, deleted=0, dry_run=True, purge_db=purge_db)
            await self._commit(task)
            return 0

        try:
            deleted += self._remove_video(task)
            deleted += self.storage.remove_job_dir(task.task_uid)
            result = await self.repo.get_result(task.tenant_id, task.id, for_update=True)
            if purge_db:
                if result is not None:
                    await self.session.delete(result)
                task.status = "failed"
                task.error_code = PURGED_ERROR_CODE
                task.error_message = PURGED_MESSAGE
            elif result is not None:
                # 默认策略：保留汇总、帧数、哈希与 summary，只清文件指针。
                result.artifact_path = None
                result.purged_at = now
        except OSError as exc:
            logger.error(
                "vision 清理文件失败 task_uid=%s error=%s",
                task.task_uid,
                type(exc).__name__,
                exc_info=True,
            )
            raise

        self._audit(task, actor=actor, deleted=deleted, dry_run=False, purge_db=purge_db)
        await self._commit(task)
        return deleted

    def _remove_video(self, task: VisionTask) -> int:
        try:
            path = self.storage.resolve(task.video_path)
        except ValueError:
            logger.warning("vision 清理跳过越界视频路径 task_uid=%s", task.task_uid)
            return 0
        if not path.is_file():
            return 0
        path.unlink()
        return 1

    def _audit(
        self,
        task: VisionTask,
        *,
        actor: str,
        deleted: int,
        dry_run: bool,
        purge_db: bool,
    ) -> None:
        self.session.add(
            AuditLog(
                tenant_id=task.tenant_id,
                user_id=None,
                action=CLEANUP_ACTION,
                resource_type="vision_task",
                resource_id=task.id,
                detail={
                    "actor": actor,
                    "task_uid": task.task_uid,
                    "deleted_files": deleted,
                    "dry_run": dry_run,
                    "purge_db": purge_db,
                },
            )
        )

    async def _commit(self, task: VisionTask) -> None:
        """逐任务提交；失败日志带 task_uid 后继续上抛，由脚本决定退出码。"""
        try:
            await self.session.commit()
        except Exception as exc:  # noqa: BLE001 - 记录后交给调用方收敛
            logger.error(
                "vision 清理提交失败 task_uid=%s error=%s",
                task.task_uid,
                type(exc).__name__,
                exc_info=True,
            )
            raise


__all__ = ["CLEANUP_ACTION", "CleanupFailed", "CleanupStats", "VisionCleanupService"]
