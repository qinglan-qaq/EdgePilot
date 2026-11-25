"""创建本地演示账号（admin / operator / viewer）。

从 ``backend`` 目录运行，密码必须显式提供，禁止在脚本或仓库里写死：

    SEED_PASSWORD='...' python scripts/seed_users.py

账号固定为：

- ``admin@edgepilot.local``：发布工作流、管理 Agent；
- ``operator@edgepilot.local``：触发运行、审批；
- ``viewer@edgepilot.local``：只读。

幂等：已存在的账号只补齐角色/状态，不覆盖已有密码；需要重置密码时加
``--reset-passwords``。全部账号落在 ``tenant_default_id`` 对应租户。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 允许 `python scripts/seed_users.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.core.tenancy import reset_tenant_id, set_tenant_id  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models.user import User  # noqa: E402

DEMO_USERS = (
    ("admin@edgepilot.local", "演示管理员", "admin"),
    ("operator@edgepilot.local", "演示操作员", "operator"),
    ("viewer@edgepilot.local", "演示只读用户", "viewer"),
)


async def seed_users(*, password: str, reset_passwords: bool) -> dict[str, int]:
    """按邮箱幂等创建演示账号，返回 created/updated/unchanged 计数。"""
    settings = get_settings()
    tenant_id = settings.tenant_default_id
    token = set_tenant_id(tenant_id)
    created = updated = unchanged = 0
    try:
        async with get_session_factory()() as session:
            for email, display_name, role in DEMO_USERS:
                user = await session.scalar(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.email == email,
                    )
                )
                if user is None:
                    session.add(
                        User(
                            tenant_id=tenant_id,
                            email=email,
                            password_hash=hash_password(password),
                            display_name=display_name,
                            role=role,
                            status="active",
                        )
                    )
                    created += 1
                    continue

                changed = False
                if user.role != role:
                    user.role = role
                    changed = True
                if user.display_name != display_name:
                    user.display_name = display_name
                    changed = True
                if user.status != "active":
                    user.status = "active"
                    changed = True
                if reset_passwords:
                    user.password_hash = hash_password(password)
                    changed = True
                if changed:
                    updated += 1
                else:
                    unchanged += 1
            await session.commit()
    finally:
        reset_tenant_id(token)
    return {"created": created, "updated": updated, "unchanged": unchanged}


def main() -> int:
    parser = argparse.ArgumentParser(description="创建 EdgePilot 本地演示账号")
    parser.add_argument(
        "--reset-passwords",
        action="store_true",
        help="同时重置已存在账号的密码",
    )
    args = parser.parse_args()

    password = os.environ.get("SEED_PASSWORD", "")
    if not password:
        print("缺少 SEED_PASSWORD 环境变量，拒绝使用默认密码。", file=sys.stderr)
        return 2
    if len(password) < 8:
        print("SEED_PASSWORD 至少 8 位。", file=sys.stderr)
        return 2

    result = asyncio.run(
        seed_users(password=password, reset_passwords=args.reset_passwords)
    )
    print(
        "演示账号就绪："
        f"created={result['created']} "
        f"updated={result['updated']} "
        f"unchanged={result['unchanged']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
