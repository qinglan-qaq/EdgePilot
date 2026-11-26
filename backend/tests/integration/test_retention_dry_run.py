"""保留策略服务与 CLI 的集成测试（P7 Task 27）。

真实测试库 + 真实会话，不 mock SQL：

- ``build_plan`` 只统计本租户过期行，180/365 天边界正确；
- ``execute_plan(dry_run=True)`` 不写库、不删截图；
- ``execute_plan(dry_run=False)`` 只删过期行，跨租户隔离，截图越界路径必须保留。
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from app.models.audit import AuditLog
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
from app.services.retention_service import build_plan, execute_plan
from tests.conftest import OTHER_TENANT, TENANT

# 固定“当前时间”，避免测试受真实时钟影响。
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

OBS_MODELS = (
    LLMCall,
    ToolCall,
    RetrievalRecord,
    PromptExecution,
    Span,
    Trace,
    AgentRun,
    WorkflowNodeRun,
    WorkflowRun,
)

RETENTION_CLI = Path(__file__).resolve().parents[3] / "scripts" / "retention.py"


def _tag() -> str:
    return uuid.uuid4().hex[:10]


async def _clear(session) -> None:
    """清掉两个租户在保留表里的历史行，保证计数断言精确（外层事务会回滚）。"""
    tenants = [TENANT, OTHER_TENANT]
    for model in (TraceEvent, VisionEvent, AuditLog, *OBS_MODELS):
        await session.execute(delete(model).where(model.tenant_id.in_(tenants)))


def _write_snapshot(root: Path, relative: str) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake-jpeg")
    return target


async def _seed_tenant(session, tenant: uuid.UUID, storage_root: Path) -> dict[str, str]:
    """为一个租户插入 100 天/400 天/新建行；返回唯一标识供断言。"""
    suffix = _tag()
    aged = NOW - timedelta(days=100)
    very_aged = NOW - timedelta(days=400)
    fresh = NOW - timedelta(days=1)

    trace_id = f"tr{suffix}"  # 12 位，满足 String(32)
    span_id = f"sp{suffix}"  # 12 位，满足 String(16)

    session.add(
        TraceEvent(
            tenant_id=tenant, trace_id=trace_id, event_type="log",
            sequence_number=1, created_at=aged,
        )
    )
    session.add(
        TraceEvent(
            tenant_id=tenant, trace_id=f"trf{suffix}", event_type="log",
            sequence_number=1, created_at=fresh,
        )
    )

    old_snapshot = f"snapshots/events/{suffix}-old.jpg"
    new_snapshot = f"snapshots/events/{suffix}-new.jpg"
    _write_snapshot(storage_root, old_snapshot)
    _write_snapshot(storage_root, new_snapshot)

    session.add(
        VisionEvent(
            tenant_id=tenant, event_type="zone", occurred_at=aged,
            dedup_key=f"old-{suffix}", snapshot_path=old_snapshot, status="new",
        )
    )
    session.add(
        VisionEvent(
            tenant_id=tenant, event_type="zone", occurred_at=fresh,
            dedup_key=f"new-{suffix}", snapshot_path=new_snapshot, status="new",
        )
    )
    # 仅主租户额外插一条越界截图路径，验证文件保留但行仍删除。
    outside_path = None
    if tenant == TENANT:
        outside_path = f"../outside-{suffix}.txt"
        (storage_root.parent / f"outside-{suffix}.txt").write_text("keep", encoding="utf-8")
        session.add(
            VisionEvent(
                tenant_id=tenant, event_type="zone", occurred_at=aged,
                dedup_key=f"outside-{suffix}", snapshot_path=outside_path, status="new",
            )
        )

    # 观测表：100 天（180 天内不删）、400 天（删）、新建（保留）。
    for age, marker in ((aged, "mid"), (very_aged, "old"), (fresh, "new")):
        session.add(
            Span(
                tenant_id=tenant, span_id=f"{span_id}{marker}"[:16],
                trace_id=trace_id, name=f"span-{marker}", span_type="agent",
                status="ok", start_time=age, created_at=age,
            )
        )
        session.add(
            Trace(
                tenant_id=tenant, trace_id=f"{marker[:1]}{suffix}", name=f"trace-{marker}",
                source="test", status="ok", start_time=age, created_at=age,
            )
        )
        session.add(
            LLMCall(
                tenant_id=tenant, span_id=span_id, provider="mock",
                model_name=f"model-{marker}", created_at=age,
            )
        )
        session.add(
            ToolCall(
                tenant_id=tenant, span_id=span_id, tool_id=f"tool-{marker}",
                tool_name="demo", execution_status="success", created_at=age,
            )
        )
        session.add(
            RetrievalRecord(
                tenant_id=tenant, span_id=span_id, query=f"q-{marker}", created_at=age,
            )
        )
        session.add(
            PromptExecution(
                tenant_id=tenant, render_status="ok", trace_id=trace_id, created_at=age,
            )
        )
        session.add(
            AgentRun(
                tenant_id=tenant, agent_id=1, status="succeeded", trace_id=trace_id,
                created_at=age,
            )
        )
        workflow_run = WorkflowRun(
            tenant_id=tenant, workflow_version_id=1, status="succeeded",
            trace_id=trace_id, created_at=age,
        )
        session.add(workflow_run)
        session.add(
            WorkflowNodeRun(
                tenant_id=tenant, run_id=1, node_id=f"{marker}-{suffix}",
                node_type="agent", status="succeeded", created_at=age,
            )
        )

    # 审计：400 天（>365 删）、300 天（<365 保留）。
    session.add(
        AuditLog(
            tenant_id=tenant, action="test.old", resource_type="test",
            created_at=NOW - timedelta(days=400),
        )
    )
    session.add(
        AuditLog(
            tenant_id=tenant, action="test.mid", resource_type="test",
            created_at=NOW - timedelta(days=300),
        )
    )

    await session.flush()
    return {
        "suffix": suffix,
        "old_snapshot": old_snapshot,
        "new_snapshot": new_snapshot,
        "outside_path": outside_path or "",
    }


async def _prepare(session, storage_root: Path) -> dict[str, dict[str, str]]:
    await _clear(session)
    data = {
        "t1": await _seed_tenant(session, TENANT, storage_root),
        "t2": await _seed_tenant(session, OTHER_TENANT, storage_root),
    }
    await session.commit()
    return data


async def _count(session, model, tenant: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant)
        )
        or 0
    )


async def _all_counts(session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tenant, prefix in ((TENANT, "t1"), (OTHER_TENANT, "t2")):
        for model in (TraceEvent, VisionEvent, AuditLog, *OBS_MODELS):
            counts[f"{prefix}:{model.__tablename__}"] = await _count(session, model, tenant)
    return counts


async def test_build_plan_counts_only_this_tenant(session, tmp_path):
    storage_root = tmp_path / "store"
    await _prepare(session, storage_root)

    plan = await build_plan(session, now=NOW, tenant_id=TENANT)
    by_key = {item.key: item for item in plan}

    assert [item.key for item in plan] == [
        "trace_events", "llm_calls", "tool_calls", "retrieval_records",
        "prompt_executions", "spans", "traces", "agent_runs",
        "workflow_node_runs", "workflow_runs", "vision_events", "audit_logs",
    ]
    # 90 天事件与 365 天审计：各 1 条过期；180 天观测表只统计 400 天的行。
    assert by_key["trace_events"].rows == 1
    assert by_key["vision_events"].rows == 2  # 100 天行 + ../outside 行
    assert by_key["audit_logs"].rows == 1
    for model in OBS_MODELS:
        table = model.__tablename__
        assert by_key[table].rows == 1, table
        assert by_key[table].days == 180
        assert by_key[table].cutoff == NOW - timedelta(days=180)

    assert by_key["trace_events"].days == 90
    assert by_key["trace_events"].cutoff == NOW - timedelta(days=90)
    assert by_key["vision_events"].cutoff == NOW - timedelta(days=90)
    # 本租户有过期截图路径的行：old.jpg + ../outside.txt
    assert by_key["vision_events"].snapshot_refs == 2
    assert by_key["audit_logs"].days == 365
    assert by_key["audit_logs"].cutoff == NOW - timedelta(days=365)
    assert sum(item.rows for item in plan) == 13

    # 未传 tenant 时按租户分组统计全部：两租户各 1 条跟踪行，截图行 3 条。
    all_plan = await build_plan(session, now=NOW)
    all_by_key = {item.key: item for item in all_plan}
    assert all_by_key["trace_events"].rows == 2
    assert all_by_key["vision_events"].rows == 3
    assert all_by_key["vision_events"].snapshot_refs == 3
    assert all_by_key["audit_logs"].rows == 2
    for model in OBS_MODELS:
        assert all_by_key[model.__tablename__].rows == 2

    # 无过期行的租户 => 全 0，空集合不报错。
    empty = await build_plan(session, now=NOW, tenant_id=uuid.uuid4())
    assert [item.rows for item in empty] == [0] * len(empty)


async def test_build_plan_custom_retention_days(session, tmp_path):
    await _prepare(session, tmp_path / "store")

    plan = await build_plan(
        session,
        now=NOW,
        tenant_id=TENANT,
        retention_days={"vision_events": 200, "obs": 1000, "audit": 500},
    )
    by_key = {item.key: item for item in plan}
    # vision 200 天：100 天行不再过期。
    assert by_key["vision_events"].rows == 0
    assert by_key["vision_events"].snapshot_refs == 0
    assert by_key["vision_events"].days == 200
    # obs 1000 天：400 天行不再过期。
    assert by_key["spans"].rows == 0
    assert by_key["workflow_runs"].rows == 0
    # audit 500 天：400 天行不再过期。
    assert by_key["audit_logs"].rows == 0
    # trace_events 未覆盖，仍按 90 天。
    assert by_key["trace_events"].rows == 1


async def test_execute_dry_run_keeps_rows_and_files(session, tmp_path):
    storage_root = tmp_path / "store"
    data = await _prepare(session, storage_root)
    before = await _all_counts(session)

    plan = await build_plan(session, now=NOW, tenant_id=TENANT)
    report = await execute_plan(session, plan, dry_run=True, storage_dir=storage_root)

    assert report.mode == "dry-run"
    assert report.total_rows == 13
    assert report.snapshot_refs == 2
    assert report.snapshot_files_deleted == 0
    assert await _all_counts(session) == before
    for key in ("old_snapshot", "new_snapshot", "outside_path"):
        relative = data["t1"][key]
        if relative:
            assert (storage_root / relative).is_file(), relative


async def test_execute_deletes_expired_rows_only(session, tmp_path):
    storage_root = tmp_path / "store"
    data = await _prepare(session, storage_root)
    outside_file = storage_root.parent / Path(data["t1"]["outside_path"]).name

    plan = await build_plan(session, now=NOW, tenant_id=TENANT)
    report = await execute_plan(session, plan, dry_run=False, storage_dir=storage_root)

    assert report.mode == "execute"
    assert report.total_rows == 13
    assert report.snapshot_files_deleted == 1  # t1-old.jpg（outside 路径被跳过）
    assert any("outside" in warning for warning in report.warnings)

    # 90 天表：过期行删除，新建行保留。
    assert await _count(session, TraceEvent, TENANT) == 1
    assert await _count(session, VisionEvent, TENANT) == 1
    # 180 天表：400 天行删除，100 天与新建行都保留。
    for model in OBS_MODELS:
        assert await _count(session, model, TENANT) == 2, model.__tablename__
    # 审计：400 天删除，300 天保留。
    assert await _count(session, AuditLog, TENANT) == 1

    # 其他租户完全未受影响。
    assert await _count(session, TraceEvent, OTHER_TENANT) == 2
    assert await _count(session, VisionEvent, OTHER_TENANT) == 2
    assert await _count(session, AuditLog, OTHER_TENANT) == 2
    for model in OBS_MODELS:
        assert await _count(session, model, OTHER_TENANT) == 3, model.__tablename__

    # 真实截图：本租户过期文件删除，新建文件与其他租户文件保留。
    assert not (storage_root / data["t1"]["old_snapshot"]).exists()
    assert (storage_root / data["t1"]["new_snapshot"]).is_file()
    assert outside_file.is_file()
    assert (storage_root / data["t2"]["old_snapshot"]).is_file()
    assert (storage_root / data["t2"]["new_snapshot"]).is_file()


async def test_execute_vision_events_in_batches(session, tmp_path, monkeypatch):
    """批大小小于过期行数时，循环要继续，删完后再以空批退出。"""
    storage_root = tmp_path / "store"
    data = await _prepare(session, storage_root)
    monkeypatch.setattr(
        "app.services.retention_service.VISION_EVENT_BATCH_SIZE", 1
    )

    plan = [
        item
        for item in await build_plan(session, now=NOW, tenant_id=TENANT)
        if item.key == "vision_events"
    ]
    report = await execute_plan(
        session, plan, dry_run=False, storage_dir=storage_root
    )

    assert report.total_rows == 2
    assert report.snapshot_files_deleted == 1
    assert await _count(session, VisionEvent, TENANT) == 1
    assert await _count(session, VisionEvent, OTHER_TENANT) == 2
    assert not (storage_root / data["t1"]["old_snapshot"]).exists()


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("retention_cli", RETENTION_CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run", "--execute"],  # 同时传
        [],                          # 都不传
    ],
)
def test_cli_requires_exactly_one_mode(argv):
    cli = _load_cli_module()
    assert cli.main(argv) == 2


def test_cli_accepts_single_mode_without_db_work():
    cli = _load_cli_module()
    parser = cli.build_parser()
    args = parser.parse_args(["--dry-run", "--tenant", str(TENANT)])
    assert args.dry_run is True
    assert args.execute is False
    assert args.tenant == str(TENANT)
    assert cli._retention_days(
        parser.parse_args(["--execute", "--events-days", "7", "--audit-days", "10"])
    ) == {"vision_events": 7, "audit": 10}
