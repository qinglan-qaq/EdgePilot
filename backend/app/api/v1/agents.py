"""Agent 路由：admin 读写，operator/viewer 只读（spec §6.3）。

发布只追加 ``agent_versions`` 行；没有 versions 的 PATCH/PUT 路由，
因此版本行在应用层不可修改。``POST /agents/{id}/test`` 仅 admin 可用
（矩阵中 Agent 配置为 admin 读写，operator/viewer 只读）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import run_agent
from app.agents.llm import LLMClient
from app.api.deps import get_current_user, require_role
from app.core.config import get_settings
from app.core.pagination import PageParams, PageResult
from app.core.tenancy import get_tenant_id
from app.db import get_db
from app.models.user import User
from app.observability import get_tracer
from app.schemas.agent import (
    AgentCreate,
    AgentOut,
    AgentRunOut,
    AgentTestIn,
    AgentUpdate,
    AgentVersionOut,
)
from app.schemas.common import ok
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def list_agents(
    params: PageParams = Depends(),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await AgentService(session).list(params.page, params.size)
    page = PageResult[AgentOut](
        items=[AgentOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.post("")
async def create_agent(
    payload: AgentCreate,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await AgentService(session).create(get_tenant_id(), **payload.model_dump())
    return ok(AgentOut.model_validate(row).model_dump())


@router.get("/{agent_id}")
async def get_agent(
    agent_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await AgentService(session).get(agent_id)
    return ok(AgentOut.model_validate(row).model_dump())


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: int,
    payload: AgentUpdate,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await AgentService(session).update(
        agent_id, **payload.model_dump(exclude_unset=True)
    )
    return ok(AgentOut.model_validate(row).model_dump())


@router.delete("/{agent_id}")
async def delete_agent(
    agent_id: int,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    await AgentService(session).delete(agent_id)
    return ok(None)


@router.post("/{agent_id}/publish")
async def publish_agent(
    agent_id: int,
    user: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await AgentService(session).publish(agent_id, created_by=user.id)
    return ok(AgentVersionOut.model_validate(row).model_dump())


@router.get("/{agent_id}/versions")
async def list_agent_versions(
    agent_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows = await AgentService(session).list_versions(agent_id)
    return ok([AgentVersionOut.model_validate(row).model_dump() for row in rows])


@router.post("/{agent_id}/test")
async def test_agent_run(
    agent_id: int,
    payload: AgentTestIn,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """管理员试跑单个 Agent；运行失败也返回 200 + ``status="failed"``。"""
    agent = await AgentService(session).get(agent_id)
    settings = get_settings()
    config = agent.config if isinstance(agent.config, dict) else {}
    fixture_name = config.get("mock_fixture")

    result = await run_agent(
        agent,
        payload.task,
        tracer=get_tracer(),
        llm=LLMClient(
            mode=settings.llm_mode,
            fixture_dir=settings.llm_fixture_dir,
            fixture_name=fixture_name if isinstance(fixture_name, str) else None,
        ),
        session=session,
        tenant_id=get_tenant_id(),
    )
    return ok(
        {
            "id": result.agent_run_id,
            "agent_id": agent.id,
            "agent_version": agent.current_version,
            "status": result.status,
            "output": result.output,
            "error": result.error,
            "trace_id": result.trace_id,
            "span_id": result.span_id,
            "total_tokens": result.total_tokens,
            "total_cost": (
                str(result.total_cost) if result.total_cost is not None else None
            ),
            "retry_count": result.retry_count,
        }
    )


@router.get("/{agent_id}/runs")
async def list_agent_runs(
    agent_id: int,
    params: PageParams = Depends(),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await AgentService(session).list_runs(
        agent_id, params.page, params.size
    )
    page = PageResult[AgentRunOut](
        items=[AgentRunOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())
