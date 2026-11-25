import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging
from app.core.metrics import (
    http_request_duration,
    http_request_total,
    render,
)
from app.core.middleware import RequestIdMiddleware, request_id_var

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "code": exc.code,
                "message": exc.message,
                "data": None,
                "trace_id": request_id_var.get(),
            },
        )

    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "message": "参数校验失败",
                "data": jsonable_encoder(exc.errors()),
                "trace_id": request_id_var.get(),
            },
        )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录 HTTP 数量与耗时；path 使用路由模板，避免高基数标签。"""

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        method = request.method
        http_request_total.labels(
            method=method, path=path, status=str(response.status_code)
        ).inc()
        http_request_duration.labels(method=method, path=path).observe(
            time.perf_counter() - started
        )
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.core.config import get_settings
        from app.db import get_session_factory
        from app.observability import get_tracer, set_writer
        from app.observability.live import LiveLayer
        from app.observability.writer import BatchWriter

        s = get_settings()
        set_writer(BatchWriter(get_session_factory(),
                               live=LiveLayer(s.redis_url, s.obs_live_ttl_seconds)))
        await get_tracer().collector.start()

        sweeper = None
        mqtt_enabled = False
        if s.mqtt_enabled:
            from app.messaging.mqtt.client import start_mqtt_service
            from app.messaging.mqtt.sweeper import NodeSweeper

            mqtt_enabled = True
            try:
                await start_mqtt_service()
            except Exception:  # noqa: BLE001 - MQTT 不得阻断 API 启动
                logger.exception("MQTT 服务启动失败，API 继续启动")
            try:
                sweeper = NodeSweeper(
                    get_session_factory(),
                    offline_after_s=s.node_offline_after_s,
                    interval_s=s.node_sweep_interval_s,
                )
                await sweeper.start()
            except Exception:  # noqa: BLE001 - 扫描任务失败不影响 API
                logger.exception("节点离线扫描启动失败")
                sweeper = None

        try:
            yield
        finally:
            # 先停 MQTT/扫描（外部消息入口），再停 tracer 采集，最后才关连接池。
            if sweeper is not None:
                try:
                    await sweeper.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("节点离线扫描停止失败")
            if mqtt_enabled:
                from app.messaging.mqtt.client import stop_mqtt_service

                try:
                    await stop_mqtt_service()
                except Exception:  # noqa: BLE001
                    logger.exception("MQTT 服务停止失败")
            await get_tracer().collector.stop()

    app = FastAPI(title="EdgePilot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.env}

    @app.get("/metrics")
    async def metrics() -> Response:
        body, content_type = render()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()
