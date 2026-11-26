"""数据保留策略服务（P7 Task 27）。

职责：

- ``build_plan`` 只做计数（``SELECT count(*)``），不拉取整表，供 ``--dry-run`` 使用；
- ``execute_plan`` 按依赖安全顺序删除，**所有 DELETE 都带显式 ``tenant_id`` 条件**；
- ``vision_events`` 先删截图文件再删行，按 1000 行分批，避免全量驻留内存；
- 截图路径统一走 :func:`app.vision.snapshot.resolve_snapshot_path`，越界/非法
  路径只跳过文件，不阻断删行；文件不存在不算错误。

保留期默认值与 spec 一致：

===============  ======  ==============
对象             保留    时间列
===============  ======  ==============
trace_events     90 天   created_at
vision_events    90 天   occurred_at
观测扩展表       180 天  created_at
spans/traces     180 天  created_at
运行记录         180 天  created_at
audit_logs       365 天  created_at
===============  ======  ==============
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.base import Base
from app.models.event import VisionEvent
from app.models.run import AgentRun, WorkflowNodeRun, WorkflowRun
from app.observability.models import (
    LLMCall,
    PromptExecution,
    RetrievalRecord,
    Span,
    ToolCall,
    Trace,
    TraceEvent,
)
from app.vision.snapshot import resolve_snapshot_path

logger = logging.getLogger(__name__)

# 默认保留天数；CLI 可通过 retention_days 覆盖。
TRACE_EVENTS_DAYS = 90
VISION_EVENTS_DAYS = 90
OBSERVABILITY_DAYS = 180
AUDIT_DAYS = 365

# vision_events 单批 id 数量：截图路径逐行解析，限制内存与事务大小。
VISION_EVENT_BATCH_SIZE = 1000

DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "trace_events": TRACE_EVENTS_DAYS,
    "vision_events": VISION_EVENTS_DAYS,
    "obs": OBSERVABILITY_DAYS,
    "audit": AUDIT_DAYS,
}


@dataclass(frozen=True)
class RetentionRule:
    """一张表的保留规则；``group`` 用于 CLI 批量覆盖默认天数。"""

    key: str
    model: type[Base]
    group: str
    time_column: str
    days: int
    label: str


# 列表顺序即删除顺序：
# 1. 观测扩展表先于 spans/traces（它们引用 span_id/trace_id，无 FK 但逻辑从属）；
# 2. agent_runs / workflow_node_runs 先于 workflow_runs；
# 3. vision_events 单独分批（需要先删截图文件）；
# 4. audit_logs 最后。
RULES: tuple[RetentionRule, ...] = (
    RetentionRule("trace_events", TraceEvent, "trace_events", "created_at",
                  TRACE_EVENTS_DAYS, "trace 时序事件"),
    RetentionRule("llm_calls", LLMCall, "obs", "created_at",
                  OBSERVABILITY_DAYS, "模型调用"),
    RetentionRule("tool_calls", ToolCall, "obs", "created_at",
                  OBSERVABILITY_DAYS, "工具调用"),
    RetentionRule("retrieval_records", RetrievalRecord, "obs", "created_at",
                  OBSERVABILITY_DAYS, "检索记录"),
    RetentionRule("prompt_executions", PromptExecution, "obs", "created_at",
                  OBSERVABILITY_DAYS, "Prompt 执行记录"),
    RetentionRule("spans", Span, "obs", "created_at",
                  OBSERVABILITY_DAYS, "span"),
    RetentionRule("traces", Trace, "obs", "created_at",
                  OBSERVABILITY_DAYS, "trace 根记录"),
    RetentionRule("agent_runs", AgentRun, "obs", "created_at",
                  OBSERVABILITY_DAYS, "Agent 运行"),
    RetentionRule("workflow_node_runs", WorkflowNodeRun, "obs", "created_at",
                  OBSERVABILITY_DAYS, "工作流节点运行"),
    RetentionRule("workflow_runs", WorkflowRun, "obs", "created_at",
                  OBSERVABILITY_DAYS, "工作流运行"),
    RetentionRule("vision_events", VisionEvent, "vision_events", "occurred_at",
                  VISION_EVENTS_DAYS, "视觉事件（含截图）"),
    RetentionRule("audit_logs", AuditLog, "audit", "created_at",
                  AUDIT_DAYS, "审计日志"),
)


def _as_utc(value: datetime) -> datetime:
    """统一成带时区时间；naive 值按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_days(rule: RetentionRule, retention_days: dict[str, int] | None) -> int:
    """解析单表保留天数：先精确表名，再按组名覆盖。"""
    if not retention_days:
        return rule.days
    if rule.key in retention_days:
        return int(retention_days[rule.key])
    if rule.group in retention_days:
        return int(retention_days[rule.group])
    return rule.days


@dataclass
class TablePlan:
    """单张表的保留计划。

    ``tenant_counts`` 是 ``build_plan`` 按租户分组统计的结果，执行时只遍历
    这些租户，确保每条 DELETE 都带租户条件；``rows`` 是各租户之和。
    """

    key: str
    table: str
    days: int
    cutoff: datetime
    rows: int = 0
    snapshot_refs: int = 0
    tenant_counts: dict[uuid.UUID, int] = field(default_factory=dict)
    with_snapshots: bool = False
    label: str = ""


@dataclass
class TableReport:
    """单表执行/预演结果。"""

    key: str
    table: str
    days: int
    cutoff: datetime
    rows: int
    snapshot_refs: int = 0
    snapshot_files_deleted: int = 0
    warnings: list[str] = field(default_factory=list)
    label: str = ""


@dataclass
class RetentionReport:
    """整体保留报告。"""

    mode: str
    tables: list[TableReport]
    tenant_id: uuid.UUID | None = None

    @property
    def total_rows(self) -> int:
        return sum(table.rows for table in self.tables)

    @property
    def snapshot_refs(self) -> int:
        return sum(table.snapshot_refs for table in self.tables)

    @property
    def snapshot_files_deleted(self) -> int:
        return sum(table.snapshot_files_deleted for table in self.tables)

    @property
    def warnings(self) -> list[str]:
        return [warning for table in self.tables for warning in table.warnings]


async def build_plan(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    tenant_id: uuid.UUID | None = None,
    retention_days: dict[str, int] | None = None,
) -> list[TablePlan]:
    """统计各表过期行数，生成删除计划；不写库、不碰文件。

    ``tenant_id=None`` 表示遍历所有租户：用 ``GROUP BY tenant_id`` 一次拿到
    各租户计数。未过期或空集合时对应表 ``rows=0``。
    """
    now_utc = _as_utc(now or datetime.now(timezone.utc))

    plan: list[TablePlan] = []
    for rule in RULES:
        days = resolve_days(rule, retention_days)
        cutoff = now_utc - timedelta(days=days)
        time_col = getattr(rule.model, rule.time_column)

        stmt = select(rule.model.tenant_id, func.count()).where(time_col < cutoff)
        if tenant_id is not None:
            stmt = stmt.where(rule.model.tenant_id == tenant_id)
        stmt = stmt.group_by(rule.model.tenant_id)

        rows = (await session.execute(stmt)).all()
        tenant_counts = {row[0]: int(row[1]) for row in rows if int(row[1]) > 0}

        snapshot_refs = 0
        if rule.key == "vision_events":
            snap_stmt = select(func.count()).select_from(rule.model).where(
                time_col < cutoff,
                rule.model.snapshot_path.is_not(None),
                rule.model.snapshot_path != "",
            )
            if tenant_id is not None:
                snap_stmt = snap_stmt.where(rule.model.tenant_id == tenant_id)
            snapshot_refs = int(await session.scalar(snap_stmt) or 0)

        plan.append(
            TablePlan(
                key=rule.key,
                table=rule.model.__tablename__,
                days=days,
                cutoff=cutoff,
                rows=sum(tenant_counts.values()),
                snapshot_refs=snapshot_refs,
                tenant_counts=tenant_counts,
                with_snapshots=rule.key == "vision_events",
                label=rule.label,
            )
        )
    return plan


async def _purge_plain_table(session: AsyncSession, plan: TablePlan, rule: RetentionRule) -> int:
    """按租户批量 DELETE + commit；每条语句都显式带 tenant_id。"""
    time_col = getattr(rule.model, rule.time_column)
    deleted = 0
    for tenant in sorted(plan.tenant_counts, key=str):
        result = await session.execute(
            delete(rule.model).where(
                rule.model.tenant_id == tenant,
                time_col < plan.cutoff,
            )
        )
        deleted += max(int(result.rowcount or 0), 0)
        await session.commit()
    return deleted


async def _delete_snapshot(
    storage_dir: str | Path | None,
    snapshot_path: str | None,
    warnings: list[str],
) -> bool:
    """删除一条事件引用的截图；成功删除返回 True。

    越界/绝对路径/空值只记录 warning 并跳过；文件不存在视为已清理。
    """
    if snapshot_path is None or not str(snapshot_path).strip():
        return False
    if storage_dir is None:
        warnings.append(f"未提供 storage_dir，跳过截图 {snapshot_path!r}（行仍会删除）")
        return False
    try:
        target = resolve_snapshot_path(storage_dir, str(snapshot_path))
    except ValueError as exc:
        warnings.append(f"跳过非法截图路径 {snapshot_path!r}: {exc}")
        logger.warning("保留清理跳过非法截图路径 path=%r error=%s", snapshot_path, exc)
        return False
    try:
        if not target.is_file():
            return False
        target.unlink()
        return True
    except OSError as exc:  # 单文件失败只告警，不阻断删除
        warnings.append(f"截图删除失败 {snapshot_path!r}: {exc}")
        logger.warning("保留清理截图删除失败 path=%r error=%s", snapshot_path, exc)
        return False


async def _purge_vision_events(
    session: AsyncSession,
    plan: TablePlan,
    *,
    storage_dir: str | Path | None,
) -> tuple[int, int, list[str]]:
    """分批清理 vision_events：先删截图文件，再按 id 删行并 commit。"""
    deleted = 0
    files_deleted = 0
    warnings: list[str] = []

    for tenant in sorted(plan.tenant_counts, key=str):
        while True:
            rows = (
                await session.execute(
                    select(VisionEvent.id, VisionEvent.snapshot_path)
                    .where(
                        VisionEvent.tenant_id == tenant,
                        VisionEvent.occurred_at < plan.cutoff,
                    )
                    .order_by(VisionEvent.id)
                    .limit(VISION_EVENT_BATCH_SIZE)
                )
            ).all()
            if not rows:
                break

            for _, snapshot_path in rows:
                if await _delete_snapshot(storage_dir, snapshot_path, warnings):
                    files_deleted += 1

            ids = [row[0] for row in rows]
            result = await session.execute(
                delete(VisionEvent).where(
                    VisionEvent.tenant_id == tenant,
                    VisionEvent.id.in_(ids),
                )
            )
            deleted_this_batch = max(int(result.rowcount or 0), 0)
            deleted += deleted_this_batch
            await session.commit()

            # 满批但实际删除 0 行（并发已删/行被锁回滚）时若无条件继续，
            # 下一轮仍会选中同样的 id，形成死循环；这里显式退出并告警。
            if deleted_this_batch == 0:
                warnings.append(
                    f"vision_events 批次删除 0 行，停止租户 {tenant} 的后续批次"
                )
                logger.warning(
                    "保留清理 vision_events 批次删除 0 行，提前结束 tenant=%s", tenant
                )
                break

            if len(rows) < VISION_EVENT_BATCH_SIZE:
                break

    return deleted, files_deleted, warnings


async def execute_plan(
    session: AsyncSession,
    plan: list[TablePlan],
    *,
    dry_run: bool = True,
    storage_dir: str | Path | None = None,
) -> RetentionReport:
    """执行或预演删除计划。

    - ``dry_run=True``（默认）：只回填计划中的计数，绝不写库、不删文件；
    - ``dry_run=False``：按计划顺序删除；DB 错误向上抛，单文件错误记录 warning；
    - 传 ``storage_dir`` 才会真正删除截图，且路径必须落在该目录内。
    """
    rules = {rule.key: rule for rule in RULES}
    reports: list[TableReport] = []

    for table_plan in plan:
        rule = rules[table_plan.key]
        if dry_run:
            reports.append(
                TableReport(
                    key=table_plan.key,
                    table=table_plan.table,
                    days=table_plan.days,
                    cutoff=table_plan.cutoff,
                    rows=table_plan.rows,
                    snapshot_refs=table_plan.snapshot_refs,
                    label=table_plan.label,
                )
            )
            continue

        if table_plan.with_snapshots:
            deleted, files_deleted, warnings = await _purge_vision_events(
                session, table_plan, storage_dir=storage_dir
            )
        else:
            deleted = await _purge_plain_table(session, table_plan, rule)
            files_deleted, warnings = 0, []

        reports.append(
            TableReport(
                key=table_plan.key,
                table=table_plan.table,
                days=table_plan.days,
                cutoff=table_plan.cutoff,
                rows=deleted,
                snapshot_refs=table_plan.snapshot_refs,
                snapshot_files_deleted=files_deleted,
                warnings=warnings,
                label=table_plan.label,
            )
        )

    return RetentionReport(mode="dry-run" if dry_run else "execute", tables=reports)
