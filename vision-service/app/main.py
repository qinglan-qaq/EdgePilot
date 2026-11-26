from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .auth import require_token
from .config import Settings
from .inference import (
    VIDEO_SUFFIXES,
    InferenceError,
    JobSpec,
    YoloEngine,
    probe_video,
)
from .jobs import JobManager
from .model import ModelRegistry
from .schemas import JobCreate, JobStateOut, ProbeIn


def _inference_error_status(code: str) -> int:
    if code == "file_too_large":
        return 413
    if code in {"video_too_long", "frame_limit_exceeded"}:
        return 422
    if code in {"video_missing", "empty_video", "decode_failed", "invalid_file_type"}:
        return 400
    if code in {"video_path_forbidden", "unauthorized"}:
        return 403
    if code == "queue_full":
        return 429
    return 500


def _validate_video_path(raw_path: str, settings: Settings) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise InferenceError("video_missing", "video_path 必须是绝对路径")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise InferenceError("video_missing", f"视频文件不存在: {path.name}") from exc
    if not resolved.is_file():
        raise InferenceError("video_missing", f"不是文件: {resolved.name}")
    if resolved.suffix.lower() not in VIDEO_SUFFIXES:
        raise InferenceError("invalid_file_type", f"不支持的视频后缀: {resolved.suffix}")
    if settings.allowed_roots and not any(
        resolved.is_relative_to(root) for root in settings.allowed_roots
    ):
        raise InferenceError("video_path_forbidden", "video_path 不在允许的媒体目录内")
    return resolved


def create_app(
    settings: Settings | None = None,
    *,
    registry: ModelRegistry | None = None,
    engine: Any | None = None,
    manager: JobManager | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    registry = registry or ModelRegistry(settings)
    engine = engine or YoloEngine(registry, settings)
    manager = manager or JobManager(settings, engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.preload:
            try:
                registry.load()
            except Exception:  # missing weights is reported through /health, not fatal
                pass
        try:
            yield
        finally:
            manager.shutdown(wait=False)

    app = FastAPI(
        title="EdgePilot Vision Service",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.manager = manager

    @app.exception_handler(InferenceError)
    async def _inference_failed(_: Request, exc: InferenceError) -> JSONResponse:
        return JSONResponse(
            status_code=_inference_error_status(exc.code),
            content={"detail": exc.message, "code": exc.code},
        )

    def _settings(request: Request) -> Settings:
        return request.app.state.settings

    def _manager(request: Request) -> JobManager:
        return request.app.state.manager

    @app.get("/health")
    async def health() -> dict[str, Any]:
        writable = settings.storage_writable()
        model = registry.info()
        status = "ok" if writable and model["load_status"] == "ready" else "degraded"
        return {
            "status": status,
            "service": settings.service_name,
            "version": __version__,
            "storage": {"writable": writable, "jobs_dir": str(settings.jobs_dir)},
            "model": model,
            "queue": manager.queue_stats(),
        }

    @app.get("/v1/models", dependencies=[Depends(require_token)])
    async def list_models() -> dict[str, Any]:
        return {"models": [registry.info()]}

    @app.post("/v1/models/reload", dependencies=[Depends(require_token)])
    async def reload_models() -> dict[str, Any]:
        return {"model": registry.reload()}

    @app.post("/v1/probe", dependencies=[Depends(require_token)])
    async def probe(payload: ProbeIn, request: Request) -> dict[str, Any]:
        return probe_video(
            _validate_video_path(payload.video_path, _settings(request)),
            _settings(request),
            registry,
        )

    @app.post("/v1/jobs", status_code=202, dependencies=[Depends(require_token)])
    async def create_job(payload: JobCreate, request: Request) -> dict[str, Any]:
        video_path = _validate_video_path(payload.video_path, _settings(request))
        spec = JobSpec(
            task_uid=payload.task_uid,
            video_path=str(video_path),
            filename=payload.filename or video_path.name,
            device_id=payload.device_id,
            video_sha256=payload.video_sha256,
            params=payload.params.model_dump(exclude_none=True),
        )
        return _manager(request).submit(spec)

    @app.get(
        "/v1/jobs/{task_uid}",
        response_model=JobStateOut,
        dependencies=[Depends(require_token)],
    )
    async def get_job(task_uid: str, request: Request) -> dict[str, Any]:
        try:
            return _manager(request).get(task_uid)
        except KeyError as exc:
            raise _not_found(task_uid) from exc

    @app.post("/v1/jobs/{task_uid}/cancel", dependencies=[Depends(require_token)])
    async def cancel_job(task_uid: str, request: Request) -> dict[str, Any]:
        try:
            return _manager(request).cancel(task_uid)
        except KeyError as exc:
            raise _not_found(task_uid) from exc

    @app.get("/v1/jobs/{task_uid}/results", dependencies=[Depends(require_token)])
    async def job_results(task_uid: str, request: Request) -> Any:
        try:
            state = _manager(request).get(task_uid)
        except KeyError as exc:
            raise _not_found(task_uid) from exc
        if state.get("status") != "completed" or not state.get("result_available"):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "任务尚未完成，暂无结果",
                    "code": "result_not_ready",
                    "status": state.get("status"),
                },
            )
        try:
            artifact = _manager(request).result(task_uid)
        except FileNotFoundError as exc:
            return JSONResponse(
                status_code=410,
                content={"detail": "结果文件已丢失", "code": "result_missing"},
            )
        return artifact

    return app


def _not_found(task_uid: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "job_not_found", "message": f"任务不存在: {task_uid}"},
    )


app = create_app()
