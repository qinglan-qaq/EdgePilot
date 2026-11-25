from fastapi import APIRouter

from app.api.v1.agents import router as agents_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.devices import router as devices_router
from app.api.v1.events import router as events_router
from app.api.v1.knowledge import router as knowledge_router
from app.api.v1.models import router as models_router
from app.api.v1.nodes import router as nodes_router
from app.api.v1.runs import router as runs_router
from app.api.v1.tools import router as tools_router
from app.api.v1.users import router as users_router
from app.api.v1.workflows import router as workflows_router
from app.observability.api.analytics import router as analytics_router
from app.observability.api.prompts import router as prompts_router
from app.observability.api.settings import router as obs_settings_router
from app.observability.api.traces import router as observability_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(dashboard_router)
api_router.include_router(users_router)
api_router.include_router(models_router)
api_router.include_router(agents_router)
api_router.include_router(tools_router)
api_router.include_router(audit_router)
api_router.include_router(nodes_router)
api_router.include_router(devices_router)
api_router.include_router(events_router)
api_router.include_router(workflows_router)
api_router.include_router(runs_router)
api_router.include_router(knowledge_router)
api_router.include_router(observability_router)
api_router.include_router(analytics_router)
api_router.include_router(prompts_router)
api_router.include_router(obs_settings_router)


@api_router.get("/ping")
async def ping() -> dict:
    return {"pong": True}
