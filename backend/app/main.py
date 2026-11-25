from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="EdgePilot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.env}

    return app


app = create_app()
