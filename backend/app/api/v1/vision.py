"""Vision Center API（P3）：视频上传、失败重试与 Agent 研判。

当前暴露的写路由：``POST /vision/tasks``（multipart 上传）、
``POST /vision/tasks/{task_uid}/retry``、``POST /vision/tasks/{task_uid}/analyze``
与 ``POST /vision/events/{event_id}/analyze``；其余 CRUD/结果/媒体路由由 Task 16
在同一模块补齐。上传的事务细节都在 ``VisionTaskService``、研判在
``VisionAgentService``，路由只做表单解析与响应包装。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.vision import VisionTaskOut, VisionZonesIn
from app.services.vision_agent_service import VisionAgentService
from app.services.vision_task_service import VisionTaskService

router = APIRouter(prefix="/vision", tags=["vision"])


class InvalidZones(AppError):
    """区域结构/坐标非法：spec 错误表约定 400 + data.reason=invalid_zones。"""

    code = http_status = 400


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


@router.post("/tasks/{task_uid}/retry")
async def retry_vision_task(
    task_uid: str,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    task = await VisionTaskService(session).retry(task_uid, user)
    return ok({"task": _task_out(task)})


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
