"""Vision 任务上传服务（P3 T11）：落盘校验、指纹幂等、并发唯一约束与派发。

事务顺序（与 plan Task 11 一致）：
临时文件 → probe（取真实 model_version）→ 有效步长与指纹 → 查重
→ 原子移动 → 插入（唯一约束兜底）→ 派发。

两条硬性约束：
1. 严禁 ``session.rollback()``。集成测试把 session 绑在调用方的外层事务上，
   回滚会连调用方一起撤销；插入竞争一律用 ``begin_nested()``（SAVEPOINT）。
2. DB 只存相对 ``VISION_STORAGE_DIR`` 的路径；派发前必须经
   ``VisionStorage.resolve()`` 转绝对路径，绝不把用户输入直接当路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from math import ceil
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, Conflict, NotFound
from app.models.user import User
from app.models.vision import VisionResult, VisionTask
from app.repositories.vision_repo import VisionRepo
from app.services.vision_client import VisionClient, VisionServiceError
from app.services.vision_storage import SavedUpload, VisionStorage

logger = logging.getLogger(__name__)

# 派发/探测失败且服务未给出明确 code 时的兜底错误码。
VISION_UNAVAILABLE = "vision_service_unavailable"
# 只有这些状态允许手动重试（任务已终态且错误可恢复）。
RETRYABLE_STATUSES = frozenset({"failed", "cancelled"})


class VideoTooLong(AppError):
    """视频时长超限：请求本身不合法，422 拒绝且不落任务（brief T11 错误表）。"""

    code = http_status = 422


class InvalidParameter(AppError):
    """上传参数超出配置上限：422 拒绝且不落任务。"""

    code = http_status = 422


def fingerprint(
    tenant_id: uuid.UUID | str,
    video_sha256: str,
    model_key: str,
    model_version: str | None,
    conf_threshold: float,
    target_samples: int,
    frame_stride: int | None,
    zones: list[dict[str, Any]] | None,
) -> str:
    """任务指纹：同一租户 + 同视频 + 同模型/参数/区域 → 同一任务。

    canonical JSON 使用 ``sort_keys``，dict 键顺序不影响结果；几何/参数任一
    变化都会改变哈希。``model_version`` 缺失（probe 失败）时用空串占位。
    """
    canonical = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "video_sha256": video_sha256,
            "model": model_key,
            "model_version": str(model_version or ""),
            "conf": round(float(conf_threshold), 3),
            "target_samples": int(target_samples),
            "frame_stride": int(frame_stride or 0),
            "zones": zones or [],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def effective_stride(
    requested: int | None, target_samples: int, probe: dict[str, Any] | None
) -> int:
    """有效步长 = max(请求值或 1, ceil(估计帧数 / target_samples))。

    vision-service 还会以 2000 的硬上限二次收敛，这里只保证抽样密度不高于
    请求值。probe 失败时没有帧数信息，直接回退到请求值/1。
    """
    stride = int(requested or 1)
    if not probe or target_samples <= 0:
        return max(stride, 1)
    frame_count = probe.get("frame_count")
    if frame_count:
        estimated = float(frame_count)
    else:
        fps = float(probe.get("fps") or 0)
        duration_ms = float(probe.get("duration_ms") or 0)
        estimated = duration_ms / 1000.0 * fps
    if estimated <= 0:
        return max(stride, 1)
    return max(stride, int(ceil(estimated / float(target_samples))))


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    return text[:limit]


@dataclass
class CreateResult:
    task: VisionTask
    deduplicated: bool


class VisionTaskService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = VisionRepo(session)

    # ---------------------------------------------------------------- upload
    async def create_upload(
        self,
        upload: UploadFile,
        *,
        conf_threshold: float,
        frame_stride: int | None,
        target_samples: int,
        zones: list[dict[str, Any]] | None,
        user: User,
        ip: str | None,
        user_agent: str | None,
    ) -> CreateResult:
        """保存视频、计算指纹并派发；派发失败只落 failed 状态，仍返回任务。

        上传本身（类型/大小/魔数）不合法时由 ``VisionStorage`` 抛 AppError，
        此时不产生任何任务行，也不调用 vision-service。

        probe 的 422（时长超限等请求级问题）同样不落任务：删除临时文件后以
        422 拒绝；只有服务不可达等环境问题才落 failed 任务供 retry。
        """
        settings = get_settings()
        tenant_id = user.tenant_id
        task_uid = str(uuid.uuid4())
        storage = self._storage(settings)

        # 1) 流式落临时文件（含 200MB 上限、后缀/魔数/空文件校验与 SHA-256）。
        saved = await storage.save_upload(upload, tenant_id, task_uid)
        filename = _truncate(Path(getattr(upload, "filename", "") or "").name, 255) or "video"
        content_type = _truncate(getattr(upload, "content_type", None), 64)

        # 2) probe 拿真实 model_version / 视频元数据；失败不阻断落库。
        client = VisionClient(settings)
        probe: dict[str, Any] | None
        probe_error: VisionServiceError | None
        try:
            probe = await client.probe(str(saved.tmp_path.resolve()))
        except VisionServiceError as exc:
            if exc.code == "video_too_long" or exc.status == 422:
                # 请求本身不合法（时长超限等）：不落任务、不派发，也不留临时文件。
                _discard(saved)
                raise VideoTooLong(
                    exc.message or "视频时长超过上限",
                    detail={"reason": "video_too_long"},
                ) from None
            probe = None
            probe_error = exc
        else:
            probe_error = None

        # 3) 后端自己的时长上限：不依赖推理服务配置，保证 VISION_MAX_VIDEO_SECONDS 生效。
        duration_ms = (probe or {}).get("duration_ms")
        if duration_ms and int(duration_ms) > int(settings.vision_max_video_seconds) * 1000:
            _discard(saved)
            raise VideoTooLong(
                f"视频时长超过 {settings.vision_max_video_seconds} 秒上限",
                detail={"reason": "video_too_long"},
            )

        model_key = settings.vision_model_key
        model_version = (probe or {}).get("model_version") or ""
        stride = effective_stride(frame_stride, int(target_samples), probe)
        dedup_key = fingerprint(
            tenant_id,
            saved.sha256,
            model_key,
            model_version,
            conf_threshold,
            int(target_samples),
            stride,
            zones,
        )

        # 4) 查重：命中既有任务则丢弃本次临时文件，不重复派发。
        existing = await self.repo.get_by_dedup(tenant_id, dedup_key)
        if existing is not None:
            _discard(saved)
            return CreateResult(existing, True)

        # 5) 原子移动：DB 里只记录相对路径。
        storage.move_into_place(saved)

        # 6) 插入；并发同指纹由唯一约束判定胜负。
        # 必须在 SAVEPOINT 内才 add 任务行：若先 add 再 begin_nested()，SAVEPOINT
        # 建立前的隐式 flush 会落在根事务上，冲突会把整个 session 打成
        # PendingRollbackError（共享事务的调用方也会被牵连）。
        fields = dict(
            tenant_id=tenant_id,
            task_uid=task_uid,
            dedup_key=dedup_key,
            status="pending",
            source_type="upload",
            video_filename=filename,
            video_path=saved.relative_path,
            video_sha256=saved.sha256,
            file_size_bytes=saved.size,
            content_type=content_type,
            conf_threshold=_decimal(round(float(conf_threshold), 3)),
            frame_stride=stride,
            target_samples=int(target_samples),
            zones=zones or None,
            model_name=(probe or {}).get("model_name") or model_key,
            model_version=model_version or None,
            video_fps=_decimal((probe or {}).get("fps")),
            video_duration_ms=(probe or {}).get("duration_ms"),
            video_width=(probe or {}).get("width"),
            video_height=(probe or {}).get("height"),
            frame_count=(probe or {}).get("frame_count"),
            created_by=getattr(user, "id", None),
        )
        try:
            async with self.session.begin_nested():
                task = self.repo.create_task(**fields)
                await self.session.flush()
        except IntegrityError:
            # SAVEPOINT 已回滚，session 仍可用；删除自己的文件，返回赢家任务。
            _discard(saved)
            existing = await self.repo.get_by_dedup(tenant_id, dedup_key)
            if existing is None:
                raise Conflict(
                    "重复上传竞争失败，请重试",
                    detail={"reason": "upload_conflict"},
                ) from None
            return CreateResult(existing, True)
        except Exception:
            _discard(saved)
            raise

        # probe 失败：任务直接落 failed，保留视频供 retry；此时不派发。
        if probe_error is not None:
            task.status = "failed"
            task.error_code = probe_error.code or VISION_UNAVAILABLE
            task.error_message = _truncate(probe_error.message or "推理服务不可达", 512)
            await self.session.commit()
            return CreateResult(task, False)

        await self.session.commit()

        # 7) 派发：结果只影响任务状态，不影响上传响应。
        await self._dispatch(task, storage, client)
        await self.session.commit()
        return CreateResult(task, False)

    # ----------------------------------------------------------------- retry
    async def retry(self, task_uid: str, user: User) -> VisionTask:
        """重试派发同一任务与视频：不改 dedup_key、不创建新任务。"""
        settings = get_settings()
        tenant_id = user.tenant_id
        task = await self.repo.get_task(tenant_id, task_uid, for_update=True)
        if task is None:
            raise NotFound("任务不存在", detail={"reason": "task_not_found"})
        if task.status not in RETRYABLE_STATUSES:
            raise Conflict(
                f"任务状态 {task.status} 不可重试",
                detail={"reason": "task_not_retryable"},
            )
        if int(task.dispatch_attempts or 0) >= int(settings.vision_max_retries):
            raise Conflict(
                f"已达 {settings.vision_max_retries} 次重试上限",
                detail={"reason": "retry_limit_exceeded"},
            )

        task.status = "pending"
        task.error_code = None
        task.error_message = None
        task.finished_at = None
        # 重试视为重新开始：清掉陈旧进度与轮询计数，避免前端展示上一轮的中间态。
        task.progress = 0
        task.started_at = None
        task.poll_failures = 0
        task.poll_misses = 0
        # 失败/取消任务通常没有结果行；若存在旧行（例如人工改状态），一并清掉，
        # 避免新 artifact 复用旧的 workflow/agent 指针。
        await self.session.execute(
            delete(VisionResult).where(
                VisionResult.tenant_id == tenant_id,
                VisionResult.task_id == task.id,
            )
        )
        await self.session.flush()

        storage = self._storage(settings)
        await self._dispatch(task, storage, VisionClient(settings))
        await self.session.commit()
        return task

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _storage(settings: Any) -> VisionStorage:
        return VisionStorage(
            settings.vision_storage_root, max_upload_mb=settings.vision_max_upload_mb
        )

    async def _dispatch(
        self, task: VisionTask, storage: VisionStorage, client: VisionClient
    ) -> None:
        """调用 vision-service 创建任务；失败只标记 failed，不抛给调用方。"""
        task.dispatch_attempts = int(task.dispatch_attempts or 0) + 1
        task.dispatched_at = datetime.now(timezone.utc)
        try:
            # 绝对路径由存储根解析得到，绝不使用用户输入拼路径。
            video_path = str(storage.resolve(task.video_path))
            await client.create_job(
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
                "vision 任务派发失败 task_uid=%s error=%s", task.task_uid, type(exc).__name__
            )
            task.status = "failed"
            task.error_code = exc.code or VISION_UNAVAILABLE
            task.error_message = _truncate(exc.message or "推理服务不可达", 512)
        else:
            task.status = "pending"


def _discard(saved: SavedUpload) -> None:
    """删除本次上传自己的临时/最终文件；不会触碰其他任务的文件。"""
    for path in (saved.tmp_path, saved.final_path):
        try:
            path.unlink(missing_ok=True)
        except OSError:  # 清理失败只记日志，不能影响上传响应。
            logger.warning("清理上传文件失败 path=%s", path)
