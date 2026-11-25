from fastapi import APIRouter

from app.api.v1.auth import router as auth_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)


@api_router.get("/ping")
async def ping() -> dict:
    return {"pong": True}
