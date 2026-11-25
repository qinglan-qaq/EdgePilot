from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware, request_id_var


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


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="EdgePilot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.env}

    return app


app = create_app()
