"""工作流 / 版本 / 运行仓储。

所有读取和写入都在这里收口，并且一律显式带 ``tenant_id``；跨租户查询返回
``None``，由 service 统一转成 404。草稿约定为 ``workflow_versions.version = 0``，
发布版本为 ``version >= 1`` 且不可修改。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.run import WorkflowNodeRun, WorkflowRun
from app.models.workflow import Workflow, WorkflowVersion
from app.observability.models.trace import Trace
from app.repositories.base import BaseRepository


class WorkflowRepo(BaseRepository[Workflow]):
    model = Workflow

    # ------------------------------------------------------------- workflows
    async def get_workflow(
        self, tenant_id: uuid.UUID, workflow_id: int
    ) -> Workflow | None:
        return await self.session.scalar(
            select(Workflow).where(
                Workflow.id == workflow_id,
                Workflow.tenant_id == tenant_id,
            )
        )

    async def get_workflow_for_update(
        self, tenant_id: uuid.UUID, workflow_id: int
    ) -> Workflow | None:
        """发布路径使用：锁住工作流行，串行化同一工作流的版本号分配。"""
        return await self.session.scalar(
            select(Workflow)
            .where(
                Workflow.id == workflow_id,
                Workflow.tenant_id == tenant_id,
            )
            .with_for_update()
        )

    async def get_by_name(
        self, tenant_id: uuid.UUID, name: str
    ) -> Workflow | None:
        return await self.session.scalar(
            select(Workflow).where(
                Workflow.tenant_id == tenant_id,
                Workflow.name == name,
            )
        )

    async def find_published_by_name(
        self, tenant_id: uuid.UUID, name: str
    ) -> Workflow | None:
        """按名称取已发布工作流；草稿或缺少发布版本时返回 ``None``。

        Vision 联动按工作流名配置触发，这里只放行可执行的已发布工作流：
        ``status == "published"`` 且 ``current_version >= 1``。未发布由调用方
        记为 skipped，而不是抛错。
        """
        workflow = await self.get_by_name(tenant_id, name)
        if workflow is None:
            return None
        if workflow.status != "published" or int(workflow.current_version or 0) < 1:
            return None
        return workflow

    async def list_workflows(
        self, tenant_id: uuid.UUID, page: int, page_size: int
    ) -> tuple[list[Workflow], int]:
        total = (
            await self.session.scalar(
                select(func.count())
                .select_from(Workflow)
                .where(Workflow.tenant_id == tenant_id)
            )
            or 0
        )
        rows = (
            await self.session.scalars(
                select(Workflow)
                .where(Workflow.tenant_id == tenant_id)
                .order_by(Workflow.created_at.desc(), Workflow.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return list(rows), total

    # -------------------------------------------------------------- versions
    async def get_draft(
        self, tenant_id: uuid.UUID, workflow_id: int
    ) -> WorkflowVersion | None:
        return await self.session.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.tenant_id == tenant_id,
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == 0,
            )
        )

    async def get_version(
        self, tenant_id: uuid.UUID, workflow_id: int, version: int
    ) -> WorkflowVersion | None:
        return await self.session.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.tenant_id == tenant_id,
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version == version,
            )
        )

    async def get_version_by_id(
        self, tenant_id: uuid.UUID, version_id: int
    ) -> WorkflowVersion | None:
        return await self.session.scalar(
            select(WorkflowVersion).where(
                WorkflowVersion.tenant_id == tenant_id,
                WorkflowVersion.id == version_id,
            )
        )

    async def max_published_version(
        self, tenant_id: uuid.UUID, workflow_id: int
    ) -> int:
        value = await self.session.scalar(
            select(func.max(WorkflowVersion.version)).where(
                WorkflowVersion.tenant_id == tenant_id,
                WorkflowVersion.workflow_id == workflow_id,
                WorkflowVersion.version >= 1,
            )
        )
        return int(value or 0)

    async def list_published_versions(
        self, tenant_id: uuid.UUID, workflow_id: int
    ) -> list[WorkflowVersion]:
        rows = (
            await self.session.scalars(
                select(WorkflowVersion)
                .where(
                    WorkflowVersion.tenant_id == tenant_id,
                    WorkflowVersion.workflow_id == workflow_id,
                    WorkflowVersion.version >= 1,
                )
                .order_by(WorkflowVersion.version.desc())
            )
        ).all()
        return list(rows)

    # ------------------------------------------------------------ last runs
    async def last_runs_by_workflow(
        self, tenant_id: uuid.UUID, workflow_ids: list[int]
    ) -> dict[int, dict]:
        """每个工作流最近一次运行（按 created_at desc, id desc）。

        用窗口函数一次取回，避免列表页 N+1；返回原始类型，供 schema 直接构造。
        """
        if not workflow_ids:
            return {}

        ranked = (
            select(
                WorkflowVersion.workflow_id.label("workflow_id"),
                WorkflowRun.id.label("run_id"),
                WorkflowRun.status.label("status"),
                WorkflowRun.created_at.label("created_at"),
                func.row_number()
                .over(
                    partition_by=WorkflowVersion.workflow_id,
                    order_by=(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()),
                )
                .label("rank"),
            )
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowVersion.tenant_id == tenant_id,
                WorkflowVersion.workflow_id.in_(workflow_ids),
            )
            .subquery()
        )
        rows = await self.session.execute(
            select(
                ranked.c.workflow_id,
                ranked.c.run_id,
                ranked.c.status,
                ranked.c.created_at,
            ).where(ranked.c.rank == 1)
        )
        return {
            row.workflow_id: {
                "id": row.run_id,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in rows
        }

    # ------------------------------------------------------------------ runs
    async def list_runs(
        self,
        tenant_id: uuid.UUID,
        page: int,
        page_size: int,
        *,
        workflow_id: int | None = None,
        status: str | None = None,
    ) -> tuple[list[tuple[WorkflowRun, str, int, int]], int]:
        where = [
            WorkflowRun.tenant_id == tenant_id,
            WorkflowVersion.tenant_id == tenant_id,
            Workflow.tenant_id == tenant_id,
        ]
        if workflow_id is not None:
            where.append(WorkflowVersion.workflow_id == workflow_id)
        if status is not None:
            where.append(WorkflowRun.status == status)

        total = (
            await self.session.scalar(
                select(func.count())
                .select_from(WorkflowRun)
                .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
                .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
                .where(*where)
            )
            or 0
        )
        result = await self.session.execute(
            select(
                WorkflowRun,
                Workflow.name,
                WorkflowVersion.workflow_id,
                WorkflowVersion.version,
            )
            .join(WorkflowVersion, WorkflowVersion.id == WorkflowRun.workflow_version_id)
            .join(Workflow, Workflow.id == WorkflowVersion.workflow_id)
            .where(*where)
            .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = [(run, name, wf_id, version) for run, name, wf_id, version in result.all()]
        return items, total

    async def get_run(
        self, tenant_id: uuid.UUID, run_id: int
    ) -> WorkflowRun | None:
        return await self.session.scalar(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id,
                WorkflowRun.tenant_id == tenant_id,
            )
        )

    async def existing_trace_ids(
        self, tenant_id: uuid.UUID, trace_ids: set[str]
    ) -> set[str]:
        """返回确实落库的 trace_id。

        ``workflow_runs.trace_id`` 只表示已分配标识；观测写入降级时可能没有
        ``traces`` 行，列表与详情据此区分“已采集”和“记录缺失”。
        """
        if not trace_ids:
            return set()
        rows = await self.session.scalars(
            select(Trace.trace_id).where(
                Trace.tenant_id == tenant_id,
                Trace.trace_id.in_(trace_ids),
            )
        )
        return set(rows)

    async def list_node_runs(
        self, tenant_id: uuid.UUID, run_id: int
    ) -> list[WorkflowNodeRun]:
        rows = (
            await self.session.scalars(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.tenant_id == tenant_id,
                    WorkflowNodeRun.run_id == run_id,
                )
                .order_by(WorkflowNodeRun.id)
            )
        ).all()
        return list(rows)


__all__ = ["WorkflowRepo"]
