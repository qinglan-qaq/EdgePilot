"""Vision Center API（T16/T18）：14 个路由、权限与媒体签名。

路由分三类：

* 读接口（health/overview/models/tasks/detail/results/events）任意登录角色可用，
  一切查询都显式带 ``tenant_id``，跨租户统一 404；
* 写接口（上传/取消/重试/触发/研判）限 admin/operator；
* ``GET /vision/tasks/{uid}/video`` 不依赖 Bearer，只用签名 token 校验
  租户 + 任务 + 视频 SHA-256。

``VISION_ENABLED=false`` 时注册在 router 上的门禁依赖让所有 ``/vision/*``
（包括无 Bearer 的视频路由）统一 503 ``vision_disabled``，其它路由不受影响。

响应统一 ``ok(...)``；业务异常全部是 ``AppError`` 子类。媒体 URL 只在详情
响应体里返回，日志不打印完整 URL/token。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    Conflict,
    Forbidden,
    NotFound,
    ServiceUnavailable,
)
from app.core.pagination import PageParams
from app.db import get_db
from app.models.user import User
from app.models.vision import VisionModel, VisionResult, VisionTask
from app.repositories.vision_repo import VisionRepo
from app.schemas.common import ok
from app.schemas.event import EventOut
from app.schemas.vision import VisionResultOut, VisionTaskOut, VisionZonesIn
from app.services.vision_agent_service import VisionAgentService
from app.services.vision_client import VisionClient, VisionServiceError
from app.services.vision_media import (
    RangeNotSatisfiable,
    parse_range,
    sign_media_token,
    stream_video,
    verify_media_token,
)
from app.services.vision_storage import (
    ArtifactCorrupted,
    VisionStorage,
    artifact_digest,
)
from app.services.vision_task_service import InvalidParameter, VisionTaskService
from app.services.vision_trigger_service import VisionTriggerService

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# 详情/列表只读这些模型字段，避免把内部列（含时间戳）无意暴露成契约。
_MODEL_FIELDS = (
    "model_key",
    "display_name",
    "provider",
    "model_version",
    "weights_filename",
    "weights_sha256",
    "task_type",
    "classes",
    "input_size",
    "license",
    "license_url",
    "commercial_use_notice",
    "status",
)


class InvalidZones(AppError):
    """区域结构/坐标非法：spec 错误表约定 400 + data.reason=invalid_zones。"""

    code = http_status = 400


async def _vision_enabled_gate() -> None:
    """请求时读取开关；关闭即 503，不依赖任何用户/租户。"""
    if not get_settings().vision_enabled:
        raise ServiceUnavailable(
            "视觉识别功能已关闭", detail={"reason": "vision_disabled"}
        )


# 门禁挂在 router 上：包括无 Bearer 的视频路由在内的全部 /vision/* 都先过开关。
router = APIRouter(
    prefix="/vision",
    tags=["vision"],
    dependencies=[Depends(_vision_enabled_gate)],
)


def _zones_error_detail(exc: ValidationError) -> dict:
    """只保留可 JSON 序列化的精简错误项。

    Pydantic v2 的 ``errors()`` 在 ``ctx.error`` 里放了 ValueError 实例，直接塞进
    AppError.detail 会让 JSONResponse 序列化失败（非信封 500），因此这里投影出
    loc/type/msg 三个纯标量字段。
    """
    errors = [
        {
            "loc": [str(part) for part in item.get("loc", ())],
            "type": str(item.get("type", "")),
            "msg": str(item.get("msg", "")),
        }
        for item in exc.errors()
    ]
    return {"reason": "invalid_zones", "errors": errors}


def _parse_zones(raw: str | None) -> list[dict] | None:
    """解析表单里的 zones JSON（可传数组或 {"zones": [...]}），并复用 schema 校验。"""
    if raw is None or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise InvalidZones("zones 必须是合法 JSON", detail={"reason": "invalid_zones"}) from None
    if isinstance(payload, dict) and "zones" in payload:
        payload = payload["zones"]
    if not isinstance(payload, list):
        raise InvalidZones("zones 必须是数组", detail={"reason": "invalid_zones"})
    try:
        parsed = VisionZonesIn(zones=payload)
    except ValidationError as exc:
        raise InvalidZones("区域结构/坐标非法", detail=_zones_error_detail(exc)) from None
    # canonical 化交给 fingerprint：这里只统一成纯 dict，避免把 pydantic 对象入库。
    return [zone.model_dump(exclude_none=True) for zone in parsed.zones]


def _task_out(task) -> dict:
    return VisionTaskOut.model_validate(task, from_attributes=True).model_dump()


def _task_item(task: VisionTask, result: VisionResult | None = None) -> dict:
    """列表/详情共用字段；``event_count`` 取结果行汇总。"""
    item = _task_out(task)
    item["event_count"] = int(result.event_count or 0) if result is not None else 0
    return item


def _result_out(result: VisionResult | None) -> dict | None:
    if result is None:
        return None
    return VisionResultOut.model_validate(result, from_attributes=True).model_dump()


def _storage() -> VisionStorage:
    settings = get_settings()
    return VisionStorage(
        settings.vision_storage_root, max_upload_mb=settings.vision_max_upload_mb
    )


def _video_file(storage: VisionStorage, task: VisionTask) -> Path | None:
    """解析视频绝对路径；越界/缺失都按“没有视频”处理。"""
    try:
        path = storage.resolve(task.video_path)
    except ValueError:
        logger.warning("vision 任务视频路径非法 task_uid=%s", task.task_uid)
        return None
    return path if path.is_file() else None


def _signed_video_url(task: VisionTask) -> tuple[str, datetime]:
    settings = get_settings()
    token = sign_media_token(
        task.tenant_id,
        task.task_uid,
        task.video_sha256,
        ttl_s=int(settings.vision_media_ttl_s),
    )
    url = (
        f"/api/v1/vision/tasks/{task.task_uid}/video"
        f"?token={token.token}&expires={token.expires_at}"
    )
    expires_at = datetime.fromtimestamp(token.expires_at, tz=timezone.utc)
    return url, expires_at


def _db_model_item(model: VisionModel) -> dict:
    return {name: getattr(model, name) for name in _MODEL_FIELDS}


def _as_items(payload: dict) -> list[dict]:
    for key in ("items", "models"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


# ---------------------------------------------------------------- read routes


@router.get("/health")
async def vision_health(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """聚合推理服务状态；不可达仍是 200，只把 ``reachable`` 置 false。"""
    settings = get_settings()
    reachable = False
    error: str | None = None
    service: dict | None = None
    model: dict | None = None
    try:
        payload = await VisionClient(settings).health()
    except VisionServiceError as exc:
        error = f"推理服务不可达: {exc.message}"
    else:
        reachable = True
        service = {
            "status": payload.get("status"),
            "service": payload.get("service"),
            "version": payload.get("version"),
            "queue": payload.get("queue"),
        }
        raw_model = payload.get("model")
        if isinstance(raw_model, dict):
            model = dict(raw_model)

    # DB 里的许可证事实优先补齐，实时服务不覆盖本地登记。
    db_model = await VisionRepo(session).get_model(
        user.tenant_id, settings.vision_model_key
    )
    if db_model is not None:
        if model is not None:
            model.setdefault("license", db_model.license)
            if db_model.commercial_use_notice:
                model.setdefault("commercial_use_notice", db_model.commercial_use_notice)
        elif reachable:
            model = {"license": db_model.license}

    return ok(
        {
            "enabled": bool(settings.vision_enabled),
            "reachable": reachable,
            "service": service,
            "model": model,
            "error": error,
        }
    )


@router.get("/overview")
async def vision_overview(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    stats = await VisionRepo(session).overview(user.tenant_id)
    return ok(
        {
            "tasks": {
                "total": stats["total_tasks"],
                "completed": stats["completed"],
                "running": stats["running"],
                "failed": stats["failed"],
            },
            "frames": {"processed_total": stats["processed_frames"]},
            "detections": {"total": stats["detection_count"]},
            "events": {"total": stats["event_count"]},
            "model": None,
            "recent_tasks": [_task_item(task) for task in stats["recent"]],
        }
    )


@router.get("/models")
async def vision_models(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """DB 模型登记 + 推理服务实时状态；服务不可达仍返回 DB 事实。"""
    repo = VisionRepo(session)
    db_items = [_db_model_item(row) for row in await repo.list_models(user.tenant_id)]

    live: dict = {"status": "unreachable", "error": None}
    try:
        payload = await VisionClient(get_settings()).models()
    except VisionServiceError as exc:
        live["error"] = f"推理服务不可达: {exc.message}"
    else:
        live = {
            "status": "ok",
            "error": None,
            "count": payload.get("count"),
            "license_notice": payload.get("license_notice"),
        }
        remote_items = _as_items(payload)
        by_key = {
            str(item.get("model_name")): item
            for item in remote_items
            if item.get("model_name")
        }
        matched: set[str] = set()
        for item in db_items:
            remote = by_key.get(str(item.get("model_key")))
            if remote is not None:
                matched.add(str(item.get("model_key")))
                item["live"] = remote
        # 服务有、DB 未登记的模型也展示，便于运维发现漂移。
        for key, remote in by_key.items():
            if key not in matched:
                db_items.append({"model_key": key, "license": remote.get("license"), "live": remote})

    return ok({"items": db_items, "count": len(db_items), "live": live})


@router.get("/tasks")
async def list_vision_tasks(
    params: PageParams = Depends(),
    status: str | None = None,
    q: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await VisionRepo(session).list_tasks(
        user.tenant_id,
        page=params.page,
        size=params.size,
        status=status,
        query=q,
        created_after=start,
        created_before=end,
    )
    return ok(
        {
            "items": [_task_item(task, result) for task, result in rows],
            "total": total,
            "page": params.page,
            "size": params.size,
        }
    )


@router.get("/tasks/{task_uid}")
async def get_vision_task(
    task_uid: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    repo = VisionRepo(session)
    task = await repo.get_task(user.tenant_id, task_uid)
    if task is None:
        raise NotFound("任务不存在", detail={"reason": "task_not_found"})
    result = await repo.get_result(user.tenant_id, task.id)

    storage = _storage()
    video_path = _video_file(storage, task)
    has_video = video_path is not None
    item = _task_item(task, result)
    # 只移除任务级计数器（processed_frames / sampled_every_n_frames），让详情
    # 载荷不携带任何帧级统计；result.summary 仍原样透传（真实 summary 可能自带
    # processed_frames），详情本身不返回 frames 数组。
    item.pop("processed_frames", None)
    item.pop("sampled_every_n_frames", None)
    item.update(
        {
            "has_video": has_video,
            "video_missing": not has_video,
            "artifact_purged": bool(result is not None and result.purged_at is not None),
            "video_url": None,
            "video_expires_at": None,
            "result": _result_out(result),
        }
    )
    if has_video:
        item["video_url"], item["video_expires_at"] = _signed_video_url(task)
    return ok(item)


@router.get("/tasks/{task_uid}/results")
async def get_vision_task_results(
    task_uid: str,
    start_ms: int | None = None,
    end_ms: int | None = None,
    class_name: str | None = None,
    min_confidence: float | None = Query(None, ge=0.0, le=1.0),
    limit: int = Query(300, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """从 artifact 读取帧级结果；文件缺失/哈希不符绝不返回空帧冒充。"""
    repo = VisionRepo(session)
    task = await repo.get_task(user.tenant_id, task_uid)
    if task is None:
        raise NotFound("任务不存在", detail={"reason": "task_not_found"})
    result = await repo.get_result(user.tenant_id, task.id)
    if result is None or result.artifact_path is None:
        raise NotFound("结果文件不存在或已被清理", detail={"reason": "artifact_missing"})

    storage = _storage()
    artifact = storage.read_artifact(
        task.task_uid,
        result.artifact_file_sha256,
        result.artifact_bytes,
        result.result_schema_version,
    )
    if result.result_sha256 and artifact_digest(artifact) != result.result_sha256:
        raise ArtifactCorrupted(
            "artifact canonical 哈希与记录不一致",
            detail={"reason": "artifact_corrupted"},
        )

    frames = artifact.get("frames") or []
    filtered: list[dict] = []
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        timestamp = int(frame.get("timestamp_ms") or 0)
        if start_ms is not None and timestamp < start_ms:
            continue
        if end_ms is not None and timestamp > end_ms:
            continue
        detections = frame.get("detections") or []
        if class_name is not None or min_confidence is not None:
            detections = [
                detection
                for detection in detections
                if (class_name is None or detection.get("class_name") == class_name)
                and (
                    min_confidence is None
                    or float(detection.get("confidence") or 0.0) >= min_confidence
                )
            ]
            if not detections:
                continue
            frame = {**frame, "detections": detections}
        filtered.append(frame)

    window = filtered[offset : offset + limit]
    video_path = _video_file(storage, task)
    return ok(
        {
            "task_uid": task.task_uid,
            "has_video": video_path is not None,
            "artifact_purged": bool(result.purged_at is not None),
            "video": artifact.get("video") or {},
            "model": artifact.get("model") or {},
            "summary": artifact.get("summary") or {},
            "query": {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "class_name": class_name,
                "min_confidence": min_confidence,
                "limit": limit,
                "offset": offset,
                "total": len(filtered),
            },
            "frames": window,
        }
    )


@router.get("/tasks/{task_uid}/events")
async def list_vision_task_events(
    task_uid: str,
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    repo = VisionRepo(session)
    task = await repo.get_task(user.tenant_id, task_uid)
    if task is None:
        raise NotFound("任务不存在", detail={"reason": "task_not_found"})
    rows, total = await repo.list_task_events(
        user.tenant_id, task.task_uid, page=params.page, size=params.size
    )
    return ok(
        {
            "items": [EventOut.from_row(row).model_dump() for row in rows],
            "total": total,
            "page": params.page,
            "size": params.size,
        }
    )


# --------------------------------------------------------------- write routes


@router.post("/tasks")
async def create_vision_task(
    request: Request,
    file: UploadFile = File(...),
    conf_threshold: float | None = Form(None, ge=0.0, le=1.0),
    frame_stride: int | None = Form(None, ge=1),
    target_samples: int | None = Form(None, ge=1),
    zones: str | None = Form(None),
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    settings = get_settings()
    # 配置上限由后端把关：超出时 422 且不落任务，不把非法参数留给推理服务。
    if frame_stride is not None and int(frame_stride) > int(settings.vision_max_frame_stride):
        raise InvalidParameter(
            f"frame_stride 不能超过 {settings.vision_max_frame_stride}",
            detail={"reason": "invalid_parameter"},
        )
    if target_samples is not None and int(target_samples) > int(settings.vision_max_samples):
        raise InvalidParameter(
            f"target_samples 不能超过 {settings.vision_max_samples}",
            detail={"reason": "invalid_parameter"},
        )
    result = await VisionTaskService(session).create_upload(
        file,
        conf_threshold=float(
            settings.vision_default_conf if conf_threshold is None else conf_threshold
        ),
        frame_stride=frame_stride,
        target_samples=int(
            settings.vision_target_frames if target_samples is None else target_samples
        ),
        zones=_parse_zones(zones),
        user=user,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok({"task": _task_out(result.task), "deduplicated": result.deduplicated})


@router.post("/tasks/{task_uid}/cancel")
async def cancel_vision_task(
    task_uid: str,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """尽力通知推理服务；不可达时仍在平台侧置 cancelled。终态幂等。"""
    repo = VisionRepo(session)
    task = await repo.get_task(user.tenant_id, task_uid, for_update=True)
    if task is None:
        raise NotFound("任务不存在", detail={"reason": "task_not_found"})
    if task.status in TERMINAL_STATUSES:
        return ok({"task": _task_out(task)})

    error_message: str | None = None
    try:
        await VisionClient(get_settings()).cancel_job(task.task_uid)
    except VisionServiceError as exc:
        logger.warning(
            "vision 取消请求失败，已在本地取消 task_uid=%s error=%s",
            task_uid,
            type(exc).__name__,
        )
        error_message = "推理服务不可达，任务已在平台侧取消"

    task.status = "cancelled"
    task.finished_at = datetime.now(timezone.utc)
    if error_message:
        task.error_message = error_message
    await session.commit()
    return ok({"task": _task_out(task)})


@router.post("/tasks/{task_uid}/retry")
async def retry_vision_task(
    task_uid: str,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    task = await VisionTaskService(session).retry(task_uid, user)
    return ok({"task": _task_out(task)})


@router.post("/tasks/{task_uid}/trigger")
async def trigger_vision_task(
    task_uid: str,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """受控重触发：pending（尚未联动）与缺失结果行拒绝，其余复用幂等运行。"""
    repo = VisionRepo(session)
    task = await repo.get_task(user.tenant_id, task_uid, for_update=True)
    if task is None:
        raise NotFound("任务不存在", detail={"reason": "task_not_found"})
    result = await repo.get_result(user.tenant_id, task.id, for_update=True)
    if result is None:
        raise Conflict("任务结果尚未生成", detail={"reason": "result_not_found"})
    if result.trigger_status == "pending":
        raise Conflict(
            "任务联动状态为 pending，等待自动触发完成后再试",
            detail={"reason": "trigger_pending"},
        )

    outcome = await VisionTriggerService(session).trigger(task, result)
    return ok(
        {
            "task_uid": task.task_uid,
            "trigger_status": outcome.status,
            "workflow_run_id": outcome.workflow_run_id,
            "trigger_error": outcome.error,
        }
    )


@router.post("/tasks/{task_uid}/analyze")
async def analyze_vision_task(
    task_uid: str,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """研判任务的最新事件；成功后同一任务复用既有运行行。"""
    result = await VisionAgentService(session).analyze_task(task_uid, user=user)
    return ok(result)


@router.post("/events/{event_id}/analyze")
async def analyze_vision_event(
    event_id: int,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """研判指定事件（落到该事件所属任务），复用规则与任务级研判一致。"""
    result = await VisionAgentService(session).analyze_event(event_id, user=user)
    return ok(result)


# --------------------------------------------------------------- media route


@router.get("/tasks/{task_uid}/video")
async def stream_vision_task_video(
    task_uid: str,
    request: Request,
    token: str | None = None,
    expires: str | None = None,
    session: AsyncSession = Depends(get_db),
) -> Response:
    """签名视频流：不依赖 Bearer；签名/租户/任务/文件任一不符统一 403。"""
    if not token or not expires:
        raise Forbidden("媒体链接无效或已过期", detail={"reason": "invalid_media_token"})

    # token 自带租户、任务与文件指纹；先按 uid 找到任务（跨租户查找仅此处使用），
    # 再用任务真实租户与 SHA-256 做恒定时间校验。
    task = await VisionRepo(session).get_task_by_uid(task_uid)
    if task is None:
        raise Forbidden("媒体链接无效或已过期", detail={"reason": "invalid_media_token"})
    verify_media_token(
        token, expires, task.tenant_id, task.task_uid, task.video_sha256
    )

    storage = _storage()
    try:
        path = storage.resolve(task.video_path)
    except ValueError:
        logger.warning("vision 视频路径非法 task_uid=%s", task.task_uid)
        raise NotFound("视频文件不存在", detail={"reason": "video_missing"}) from None
    if not path.is_file():
        raise NotFound("视频文件不存在", detail={"reason": "video_missing"})

    size = path.stat().st_size
    try:
        byte_range = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        # 416 需要 Content-Range 头，AppError 信封不带头，直接返回空响应。
        return Response(
            status_code=416,
            headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes */{size}"},
        )
    return stream_video(path, byte_range)
