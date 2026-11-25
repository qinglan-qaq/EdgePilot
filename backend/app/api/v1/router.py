from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/ping")
async def ping() -> dict:
    return {"pong": True}
