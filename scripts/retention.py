"""数据保留脚本（P7 Task 27）。

从仓库根目录运行：

    python scripts/retention.py --dry-run                 # 只统计，不删除
    python scripts/retention.py --execute                 # 真正删除（默认全租户）
    python scripts/retention.py --execute --tenant <uuid>  # 只处理单个租户
    python scripts/retention.py --dry-run --events-days 30 --audit-days 730

安全约定：

- ``--dry-run`` 与 ``--execute`` 必须二选一；都不传或同时传都以 exit 2 退出，
  脚本绝不会隐式删除任何数据；
- 所有 DELETE 在服务层按租户分组执行，未传 ``--tenant`` 时遍历所有租户；
- ``--storage-dir`` 默认取 ``settings.storage_dir``，截图路径越界只跳过文件。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

# 从仓库根运行 ``python scripts/retention.py`` 时需要能 import app.*
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db import get_engine, get_session_factory  # noqa: E402
from app.services.retention_service import (  # noqa: E402
    RetentionReport,
    build_plan,
    execute_plan,
)


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("天数不能为负数")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 解析器；--dry-run/--execute 互斥且必选。"""
    parser = argparse.ArgumentParser(
        description="EdgePilot 数据保留清理：删除过期事件、观测数据与审计日志"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将删除的行数，不写库、不删文件（默认模式）",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="真正执行删除（危险操作，请先跑 --dry-run）",
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="只处理指定租户 UUID；缺省遍历所有租户",
    )
    parser.add_argument(
        "--storage-dir",
        default=None,
        help="截图根目录（默认 settings.storage_dir）",
    )
    parser.add_argument(
        "--trace-events-days",
        type=_non_negative_int,
        default=None,
        help="trace_events 保留天数（默认 90）",
    )
    parser.add_argument(
        "--events-days",
        type=_non_negative_int,
        default=None,
        help="vision_events（含截图）保留天数（默认 90）",
    )
    parser.add_argument(
        "--obs-days",
        type=_non_negative_int,
        default=None,
        help="spans/traces/llm_calls/tool_calls 等观测表保留天数（默认 180）",
    )
    parser.add_argument(
        "--audit-days",
        type=_non_negative_int,
        default=None,
        help="audit_logs 保留天数（默认 365）",
    )
    return parser


def _retention_days(args: argparse.Namespace) -> dict[str, int] | None:
    mapping = {
        "trace_events": args.trace_events_days,
        "vision_events": args.events_days,
        "obs": args.obs_days,
        "audit": args.audit_days,
    }
    overrides = {key: value for key, value in mapping.items() if value is not None}
    return overrides or None


def _print_report(report: RetentionReport, *, tenant_id: uuid.UUID | None) -> None:
    tenant_label = str(tenant_id) if tenant_id else "ALL"
    print(f"[retention] mode={report.mode} tenant={tenant_label}")
    for table in report.tables:
        extra = ""
        if table.key == "vision_events":
            extra = (
                f" snapshot_refs={table.snapshot_refs}"
                f" snapshot_deleted={table.snapshot_files_deleted}"
            )
        print(
            f"[retention] table={table.table} label={table.label} "
            f"days={table.days} cutoff={table.cutoff.isoformat()} "
            f"rows={table.rows}{extra}"
        )
        for warning in table.warnings:
            print(f"[retention] WARN {table.table}: {warning}")
    print(
        f"[retention] summary: total_rows={report.total_rows} "
        f"snapshot_refs={report.snapshot_refs} "
        f"snapshot_files_deleted={report.snapshot_files_deleted} "
        f"warnings={len(report.warnings)} mode={report.mode}"
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    tenant_id = uuid.UUID(str(args.tenant)) if args.tenant else None
    storage_dir = args.storage_dir or settings.storage_dir
    now = datetime.now(timezone.utc)

    async with get_session_factory()() as session:
        plan = await build_plan(
            session,
            now=now,
            tenant_id=tenant_id,
            retention_days=_retention_days(args),
        )
        report = await execute_plan(
            session,
            plan,
            dry_run=args.dry_run,
            storage_dir=storage_dir,
        )
    _print_report(report, tenant_id=tenant_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    """入口；参数错误返回 2，运行期错误返回 1，成功返回 0。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse 的 --help / 参数互斥错误
        return int(exc.code or 0)

    async def _guarded() -> int:
        try:
            return await _run(args)
        finally:
            await get_engine().dispose()

    try:
        return asyncio.run(_guarded())
    except Exception as exc:  # noqa: BLE001 - CLI 必须给出明确失败信息
        print(f"[retention] ERROR: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
