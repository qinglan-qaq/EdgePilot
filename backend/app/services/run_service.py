"""工作流运行业务逻辑：创建 pending 运行、查询、取消。

运行创建只落库，不触发执行、不伪造 ``trace_id``：worker 领取后才会写
``running`` / span / 节点结果。创建时按 DSL 节点声明顺序预插全部
``workflow_node_runs``（status=pending, attempt=1），供后续引擎幂等推进。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound
from app.core.tenancy import get_tenant_id
from app.models.run import WorkflowNodeRun, WorkflowRun
from app.repositories.workflow_repo import WorkflowRepo
from app.services.audit_service import AuditService
from app.workflow.dsl import nodes_of

NON_TERMINAL_STATUSES = frozenset({"pending", "running", "waiting_approval"})


@dataclass
class RunContext:
    run: WorkflowRun
    workflow_id: int
    workflow_name: str
    dsl: dict
    nodes: list[WorkflowNodeRun]


@dataclass
class RunListRow:
    run: WorkflowRun
    workflow_id: int
    workflow_name: str


def order_node_runs(
    node_runs: list[WorkflowNodeRun], dsl: dict[str, Any]
) -> list[WorkflowNodeRun]:
    """按 DSL 节点声明顺序排序；DSL 中不存在的节点排末尾并按 node_id 排序。"""
    order: dict[str, int] = {}
    for index, node in enumerate(nodes_of(dsl)):
        node_id = node.get("id")
        if isinstance(node_id, str):
            order.setdefault(node_id, index)
    fallback = len(order) + len(nodes_of(dsl))
    return sorted(
        node_runs,
        key=lambda row: (
            order.get(row.node_id, fallback),
            row.node_id,
        ),
    )


class RunService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = WorkflowRepo(session)

    # ----------------------------------------------------------------- create
    async def create(
        self,
        workflow_id: int,
        *,
        input_data: dict | None = None,
        trigger_type: str = "manual",
        trigger_payload: dict | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RunContext:
        tenant_id = get_tenant_id()
        workflow = await self.repo.get_workflow(tenant_id, workflow_id)
        if workflow is None:
            raise NotFound("工作流不存在")
        if workflow.status != "published" or workflow.current_version < 1:
            raise Conflict("工作流尚未发布")

        version = await self.repo.get_version(
            tenant_id, workflow_id, workflow.current_version
        )
        if version is None:
            raise Conflict("工作流已发布版本不存在")

        run = WorkflowRun(
            tenant_id=tenant_id,
            workflow_version_id=version.id,
            trigger_type=trigger_type,
            trigger_payload=trigger_payload,
            status="pending",
            input=input_data,
            trace_id=None,
        )
        self.session.add(run)
        # flush 只为拿到自增 run.id；仍在同一事务内，节点行随后一起提交。
        await self.session.flush()

        for node in nodes_of(version.dsl):
            self.session.add(
                WorkflowNodeRun(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    node_id=node["id"],
                    node_type=node["type"],
                    status="pending",
                    attempt=1,
                )
            )
        await self.session.commit()
        await self.session.refresh(run)

        await AuditService.write(
            self.session,
            action="workflow.run",
            resource_type="workflow",
            resource_id=workflow_id,
            detail={
                "workflow_id": workflow_id,
                "run_id": run.id,
                "version": workflow.current_version,
                "trigger_type": trigger_type,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return await self.detail(run.id)

    # ------------------------------------------------------------------- read
    async def list(
        self,
        page: int = 1,
        page_size: int = 20,
        *,
        workflow_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[RunListRow], int]:
        tenant_id = get_tenant_id()
        rows, total = await self.repo.list_runs(
            tenant_id,
            page,
            page_size,
            workflow_id=workflow_id,
            status=status,
        )
        return [
            RunListRow(run=run, workflow_name=name, workflow_id=wf_id)
            for run, name, wf_id in rows
        ], total

    async def detail(self, run_id: int) -> RunContext:
        tenant_id = get_tenant_id()
        run = await self.repo.get_run(tenant_id, run_id)
        if run is None:
            raise NotFound("运行不存在")

        version = await self.repo.get_version_by_id(
            tenant_id, run.workflow_version_id
        )
        if version is None:
            raise NotFound("运行版本不存在")
        workflow = await self.repo.get_workflow(tenant_id, version.workflow_id)
        if workflow is None:
            raise NotFound("运行所属工作流不存在")

        nodes = await self.repo.list_node_runs(tenant_id, run.id)
        return RunContext(
            run=run,
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            dsl=version.dsl,
            nodes=order_node_runs(nodes, version.dsl),
        )

    @staticmethod
    def logs(ctx: RunContext) -> list[WorkflowNodeRun]:
        """节点级时间线；没有节点记录时就是空数组，不编造。"""
        return list(ctx.nodes)

    # ----------------------------------------------------------------- cancel
    async def cancel(
        self,
        run_id: int,
        *,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> RunContext:
        ctx = await self.detail(run_id)
        run = ctx.run
        if run.status not in NON_TERMINAL_STATUSES:
            raise Conflict("运行已结束，无法取消")

        previous = run.status
        now = datetime.now(timezone.utc)
        run.status = "cancelled"
        run.finished_at = now
        for node in ctx.nodes:
            if node.status in NON_TERMINAL_STATUSES:
                node.status = "skipped"
                node.finished_at = now
        await self.session.commit()

        await AuditService.write(
            self.session,
            action="workflow.cancel",
            resource_type="workflow_run",
            resource_id=run_id,
            detail={
                "run_id": run_id,
                "workflow_id": ctx.workflow_id,
                "from_status": previous,
            },
            user=user,
            ip=ip,
            user_agent=user_agent,
        )
        return await self.detail(run_id)


__all__ = [
    "NON_TERMINAL_STATUSES",
    "RunContext",
    "RunListRow",
    "RunService",
    "order_node_runs",
]
