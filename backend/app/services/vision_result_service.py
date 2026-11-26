"""Vision artifact 入库与「先结果、后完成」（Task 12）。

硬性顺序（不可调换）：

1. ``read_artifact``：文件字节 SHA-256、canonical SHA-256、schema_version 三重校验；
2. ``upsert_result`` 并提交：结果先落库，此刻任务仍未 ``completed``；
3. 事件生成并提交：只计真正新增的行；
4. 任务 ``completed`` 并提交：完成必须晚于结果落库；
5. 触发联动（Task 17）：只回写 ``trigger_status/trigger_error``，失败不回退任务。

artifact 校验失败一律把任务置 ``failed`` 并重新抛出，绝不写空结果或用默认值
掩盖缺失的证据。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.vision import VisionResult, VisionTask
from app.repositories.vision_repo import VisionRepo
from app.services import vision_event_service
from app.services.vision_storage import (
    ArtifactCorrupted,
    ArtifactMissing,
    ArtifactUnreadable,
    VisionStorage,
    artifact_digest,
)
from app.services.vision_trigger_service import (
    VisionTriggerService,
    apply_trigger_outcome,
)

logger = logging.getLogger(__name__)

# artifact 级失败映射到任务的 error_code；轮询器/前端据此给出可操作提示。
ARTIFACT_ERROR_CODES: dict[type[BaseException], str] = {
    ArtifactMissing: "artifact_missing",
    ArtifactCorrupted: "artifact_corrupted",
    ArtifactUnreadable: "artifact_unreadable",
}
_ARTIFACT_ERRORS = tuple(ARTIFACT_ERROR_CODES)
_ERROR_MESSAGE_LIMIT = 512


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class VisionResultService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = VisionRepo(session)

    async def ingest(self, task: VisionTask, remote_state: dict[str, Any]) -> VisionResult:
        """按 artifact 证据写入结果、事件并完成任务，最后做联动。

        Args:
            task: 当前租户的视觉任务；调用方负责事务与租户上下文。
            remote_state: vision-service 的 completed 状态（哈希、大小、schema）。

        Returns:
            已落库并带联动状态的结果行。

        Raises:
            ArtifactMissing: 结果文件不存在或已被清理。
            ArtifactCorrupted: 文件大小/哈希与任务记录不一致。
            ArtifactUnreadable: 文件无法解析、schema 不符或缺少 frames。
        """
        settings = get_settings()
        storage = VisionStorage(
            settings.vision_storage_root, max_upload_mb=settings.vision_max_upload_mb
        )

        try:
            artifact = self._read_verified(task, remote_state, storage)
            frames = artifact["frames"]
        except _ARTIFACT_ERRORS as exc:
            await self._mark_failed(task, exc)
            raise

        summary = artifact.get("summary") or {}
        video = artifact.get("video") or {}
        model = artifact.get("model") or {}
        detection_count = _int_or_none(summary.get("detection_count")) or 0

        # 重放时保留既有事件计数；首次入库从 0 开始，等事件生成后再回写。
        existing = await self.repo.get_result(task.tenant_id, task.id)
        event_count = int(existing.event_count or 0) if existing is not None else 0

        # 1) 结果先落库。canonical（result_sha256）与文件字节
        #    （artifact_file_sha256）必须同时保存，二者语义不同。
        result = await self.repo.upsert_result(
            task.tenant_id,
            task.id,
            task_uid=task.task_uid,
            artifact_path=storage.artifact_relative(task.task_uid),
            artifact_bytes=int(remote_state["result_bytes"]),
            artifact_file_sha256=str(remote_state["result_file_sha256"]),
            result_sha256=artifact_digest(artifact),
            result_schema_version=artifact.get("schema_version"),
            frame_count=len(frames),
            detection_count=detection_count,
            class_counts=summary.get("class_counts") or {},
            peak_confidence=_decimal(summary.get("peak_confidence")),
            inference_ms=_int_or_none(summary.get("inference_ms")),
            event_count=event_count,
            summary=summary,
        )
        await self.session.commit()

        # 2) 事件在 completed 之前落库；重放命中 dedup_key 时新增 0 行。
        created_events = await vision_event_service.generate(self.session, task, artifact)
        if created_events > 0 or event_count == 0:
            result.event_count = created_events
        await self.session.commit()

        # 3) 最后才写 completed，保证「完成」永远晚于结果与事件。
        #    重放（任务已完成且已有 finished_at）不得重复完成：跳过完成块，
        #    否则 finished_at 会被推到本次轮询时间。
        if task.status != "completed" or task.finished_at is None:
            now = datetime.now(timezone.utc)
            task.status = "completed"
            task.progress = 100
            task.processed_frames = (
                _int_or_none(summary.get("processed_frames")) or len(frames)
            )
            task.detection_count = detection_count
            task.frame_count = len(frames)
            task.inference_ms = _int_or_none(summary.get("inference_ms"))
            task.video_fps = _decimal(video.get("fps"))
            task.video_duration_ms = _int_or_none(video.get("duration_ms"))
            task.video_width = _int_or_none(video.get("width"))
            task.video_height = _int_or_none(video.get("height"))
            task.sampled_every_n_frames = _int_or_none(
                video.get("sampled_every_n_frames")
            )
            task.model_name = model.get("model_name") or task.model_name
            task.model_version = model.get("model_version") or task.model_version
            task.device = model.get("device") or task.device
            task.error_code = None
            task.error_message = None
            task.finished_at = now
            await self.session.commit()

        # 4) 联动在 completed 之后；任何异常只落 trigger_status，不回退任务。
        await self._trigger(task, result)
        return result

    # ---------------------------------------------------------------- helpers
    def _read_verified(
        self,
        task: VisionTask,
        remote_state: dict[str, Any],
        storage: VisionStorage,
    ) -> dict[str, Any]:
        missing = [
            key
            for key in (
                "result_file_sha256",
                "result_bytes",
                "result_schema_version",
                "result_sha256",
            )
            if remote_state.get(key) in (None, "")
        ]
        if missing:
            raise ArtifactUnreadable(
                f"completed 状态缺少证据字段: {', '.join(missing)}",
                detail={"reason": "artifact_unreadable", "missing": missing},
            )

        artifact = storage.read_artifact(
            task.task_uid,
            str(remote_state["result_file_sha256"]),
            int(remote_state["result_bytes"]),
            str(remote_state["result_schema_version"]),
        )
        canonical = artifact_digest(artifact)
        if canonical != str(remote_state["result_sha256"]):
            raise ArtifactCorrupted(
                "artifact canonical 哈希不符",
                detail={"reason": "artifact_corrupted"},
            )
        frames = artifact.get("frames")
        # frames 为空表示没有采样结果，属于不可用证据；frames 非空但 detections
        # 全空是合法的「无目标」，可以 detection_count=0 完成。
        if not isinstance(frames, list) or not frames:
            raise ArtifactUnreadable(
                "artifact 缺少 frames，拒绝以空结果完成",
                detail={"reason": "artifact_unreadable"},
            )
        return artifact

    async def _mark_failed(
        self, task: VisionTask, exc: BaseException
    ) -> None:
        task.status = "failed"
        task.error_code = ARTIFACT_ERROR_CODES.get(type(exc), "artifact_unreadable")
        task.error_message = (str(exc).strip() or type(exc).__name__)[
            :_ERROR_MESSAGE_LIMIT
        ]
        task.finished_at = datetime.now(timezone.utc)
        await self.session.commit()

    async def _trigger(self, task: VisionTask, result: VisionResult) -> None:
        """调用 Task 17 的触发服务；失败只记录，不影响已完成的结果。

        真实触发服务已经写回并提交结果行；Fake/其它实现只返回 outcome 时，
        这里补写一次。联动字段只在一处落库，避免每次轮询两条 UPDATE。
        写入与提交都放在 try 内，联动失败绝不从 ``ingest`` 抛出。
        """
        # 失败路径可能让 session 过期实例，先取出稳定标量。
        task_uid = task.task_uid
        try:
            outcome = await VisionTriggerService(self.session).trigger(task, result)
            if (
                result.trigger_status,
                result.workflow_run_id,
                result.trigger_error,
            ) != (outcome.status, outcome.workflow_run_id, outcome.error):
                apply_trigger_outcome(
                    result, outcome.status, outcome.workflow_run_id, outcome.error
                )
                await self.session.commit()
        except Exception as exc:  # noqa: BLE001 - 联动异常不能回滚已落库结果
            logger.warning(
                "视觉任务联动异常 task_uid=%s error=%s",
                task_uid,
                type(exc).__name__,
                exc_info=True,
            )
            try:
                apply_trigger_outcome(
                    result, "failed", None, str(exc).strip() or type(exc).__name__
                )
                await self.session.commit()
            except Exception:  # noqa: BLE001 - 回写失败也不能影响已入库结果
                logger.warning(
                    "视觉任务联动状态回写失败 task_uid=%s", task_uid, exc_info=True
                )


__all__ = ["ARTIFACT_ERROR_CODES", "VisionResultService"]
