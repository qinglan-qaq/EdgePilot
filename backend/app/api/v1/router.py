from fastapi import APIRouter

from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.observability.api.prompts import router as prompts_router
from app.observability.api.traces import router as observability_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(audit_router)
api_router.include_router(observability_router)
api_router.include_router(prompts_router)


@api_router.get("/ping")
async def ping() -> dict:
    return {"pong": True}
