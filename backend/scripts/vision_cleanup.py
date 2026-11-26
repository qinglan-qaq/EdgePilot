"""Vision 清理脚本（T19）：运维侧删除过期视频与 artifact，保留汇总并审计。

用法::

    python scripts/vision_cleanup.py --days 90 --actor ops@example.com
    python scripts/vision_cleanup.py --days 90 --actor ops@example.com --dry-run
    python scripts/vision_cleanup.py --days 90 --actor ops@example.com \
        --purge-db --confirm

安全约束：

* ``--actor`` 必填，写入每条 ``audit_logs`` 的 ``detail.actor``；
* ``--purge-db`` 是显式破坏性操作，必须同时带 ``--confirm``，否则退出码 2；
* HTTP 不提供清理接口，只能由具备数据库写权限的运维账号执行本脚本；
* 成功退出 0，意外失败退出 1 并在日志中给出 ``task_uid``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.vision_cleanup_service import (  # noqa: E402
    CleanupFailed,
    VisionCleanupService,
)

logger = logging.getLogger("vision_cleanup")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete expired vision media, keep summaries, write audit logs."
    )
    parser.add_argument("--days", type=int, default=90, help="retention days (default 90)")
    parser.add_argument(
        "--actor",
        required=True,
        help="operator email recorded in audit detail.actor (required)",
    )
    parser.add_argument("--dry-run", action="store_true", help="count only, delete nothing")
    parser.add_argument(
        "--purge-db",
        action="store_true",
        help="also delete vision_results rows and mark tasks failed(purged)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="required together with --purge-db",
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    settings = get_settings()
    url = os.environ.get("DATABASE_URL") or settings.database_url
    engine = create_async_engine(url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            stats = await VisionCleanupService(
                session, str(settings.vision_storage_root)
            ).run(
                days=args.days,
                dry_run=args.dry_run,
                purge_db=args.purge_db,
                actor=args.actor,
            )
    finally:
        await engine.dispose()
    return asdict(stats)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args()

    if args.purge_db and not args.confirm:
        parser.error("--purge-db requires --confirm (destructive operation refused)")
        return 2  # pragma: no cover - parser.error raises SystemExit(2)

    try:
        result = asyncio.run(_run(args))
    except CleanupFailed as exc:
        logger.error("vision cleanup failed at task_uid=%s", exc.task_uid)
        print(f"failed: task_uid={exc.task_uid}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 脚本边界：记录并返回非零退出码
        logger.error("vision cleanup failed: %s", exc, exc_info=True)
        print(f"failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
