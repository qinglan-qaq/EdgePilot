"""登记 Vision Center 的 YOLO11n 模型元数据（幂等）。

从 ``backend`` 目录运行：

    python scripts/seed_vision_demo.py
    python scripts/seed_vision_demo.py --weights /path/to/yolo11n.pt

只写 ``vision_models`` 中默认租户的 ``yolo11n`` 一行（DB 仅登记元数据，不存
权重文件本身），不创建任务、不生成结果、不写事件。权重存在时计算 SHA-256 并
把 ``model_version`` 记为 ``yolo11n-<sha256[:12]>``；权重缺失时保留可核验的
回退版本号 ``yolo11n``，并在 ``notes`` 中明确写“权重未就位”。

许可提示固定为 AGPL-3.0：可遵守 AGPL 义务使用，或向 Ultralytics 购买
Enterprise License；本仓库不默认授予商业使用权限。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

# 允许 `python scripts/seed_vision_demo.py` 直接运行
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.tenancy import reset_tenant_id, set_tenant_id  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.repositories.vision_repo import VisionRepo  # noqa: E402

MODEL_KEY = "yolo11n"
DEFAULT_WEIGHTS = REPO_ROOT / "vision-service" / "weights" / "yolo11n.pt"
CHUNK_SIZE = 1024 * 1024

LICENSE_NAME = "AGPL-3.0"
LICENSE_URL = "https://www.ultralytics.com/license"
# 与 vision-service/app/config.py 的 COMMERCIAL_NOTICE 保持同一措辞。
COMMERCIAL_NOTICE = (
    "Ultralytics YOLO 采用 AGPL-3.0 许可。将本服务用于闭源商业产品或对外提供"
    "网络服务前，请自行评估 AGPL 义务或购买 Ultralytics 商业许可；本仓库不默认"
    "授予商业使用权限。"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path) -> str:
    """仓库内路径显示为相对路径，仓库外保留绝对路径，便于直接复制。"""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def build_fields(weights: Path) -> tuple[dict[str, Any], str]:
    """按权重是否就位生成 ``vision_models`` 字段，返回 (fields, 模式说明)。"""
    filename = weights.name
    if weights.is_file():
        digest = sha256_file(weights)
        model_version = f"{MODEL_KEY}-{digest[:12]}"
        fields: dict[str, Any] = {
            "display_name": "YOLO11n（Ultralytics）",
            "provider": "ultralytics",
            "model_version": model_version,
            "weights_filename": filename,
            "weights_sha256": digest,
            "task_type": "detection",
            "input_size": 640,
            "license": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "commercial_use_notice": COMMERCIAL_NOTICE,
            "status": "available",
            "notes": (
                f"权重路径 {_display_path(weights)}；数据库仅登记元数据，"
                "权重文件由 fetch_model.py 下载且不提交仓库。"
            ),
        }
        mode = (
            f"权重就位: {_display_path(weights)}，"
            f"model_version={model_version}（sha256 前 12 位）"
        )
        return fields, mode

    fields = {
        "display_name": "YOLO11n（Ultralytics）",
        "provider": "ultralytics",
        # 无 SHA-256 时不编造版本号，保留模型键作为可核验的回退值。
        "model_version": MODEL_KEY,
        "weights_filename": filename,
        "weights_sha256": None,
        "task_type": "detection",
        "input_size": 640,
        "license": LICENSE_NAME,
        "license_url": LICENSE_URL,
        "commercial_use_notice": COMMERCIAL_NOTICE,
        "status": "available",
        "notes": (
            f"权重未就位：{_display_path(weights)}；数据库仅登记元数据，"
            "请先在 vision-service 运行 scripts/fetch_model.py 并复制权重。"
        ),
    }
    mode = f"权重未就位: {_display_path(weights)}，回退 model_version={MODEL_KEY}"
    return fields, mode


async def seed_vision_model(
    session: AsyncSession,
    *,
    tenant_id: Any,
    weights: Path,
) -> dict[str, Any]:
    """幂等 upsert 默认租户的 yolo11n 行；返回 created/updated/unchanged 计数。"""
    fields, mode = build_fields(weights)
    repo = VisionRepo(session)
    existing = await repo.get_model(tenant_id, MODEL_KEY)

    if existing is None:
        action = "created"
        created, updated, unchanged = 1, 0, 0
    else:
        changed = {
            key: value
            for key, value in fields.items()
            if getattr(existing, key) != value
        }
        if changed:
            action = "updated"
            created, updated, unchanged = 0, 1, 0
        else:
            action = "unchanged"
            created, updated, unchanged = 0, 0, 1

    # 不写 last_checked_at：本脚本只登记元数据，第二次运行应与第一次完全一致。
    model = await repo.upsert_model(tenant_id, MODEL_KEY, **fields)
    return {
        "tenant_id": str(tenant_id),
        "model_key": MODEL_KEY,
        "action": action,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "model_version": model.model_version,
        "weights_sha256": model.weights_sha256,
        "weights_path": _display_path(weights),
        "mode": mode,
    }


def print_license_notice() -> None:
    print()
    print("=" * 72)
    print("模型许可：AGPL-3.0（Ultralytics YOLO11n 预训练权重）")
    print(f"许可说明：{LICENSE_URL}")
    print("两条授权路径：")
    print("  1) 遵守 AGPL-3.0 义务（含对外提供网络服务时的源码开放要求）；")
    print("  2) 向 Ultralytics 购买 Enterprise License。")
    print("不默认授予商业使用权限：闭源商用/对外服务/分发前请自行评估并取得授权。")
    print("=" * 72)


async def _run(weights: Path) -> dict[str, Any]:
    settings = get_settings()
    tenant_id = settings.tenant_default_id
    token = set_tenant_id(tenant_id)
    try:
        async with get_session_factory()() as session:
            stats = await seed_vision_model(
                session, tenant_id=tenant_id, weights=weights
            )
            await session.commit()
    finally:
        reset_tenant_id(token)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="登记 Vision Center 的 yolo11n 模型元数据（幂等）"
    )
    parser.add_argument(
        "--weights",
        type=Path,
        default=DEFAULT_WEIGHTS,
        help=f"yolo11n.pt 路径（默认: {DEFAULT_WEIGHTS}）",
    )
    args = parser.parse_args()

    weights = args.weights
    if not weights.is_absolute():
        weights = (REPO_ROOT / weights).resolve()

    try:
        stats = asyncio.run(_run(weights))
    except Exception as exc:  # noqa: BLE001 - 脚本边界：给出明确原因并非零退出
        print(
            f"seed_vision_demo 失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        "vision_models 就绪："
        f"tenant_id={stats['tenant_id']} "
        f"model_key={stats['model_key']} "
        f"action={stats['action']} "
        f"created={stats['created']} "
        f"updated={stats['updated']} "
        f"unchanged={stats['unchanged']}"
    )
    print(f"模式：{stats['mode']}")
    print(f"model_version={stats['model_version']} weights_sha256={stats['weights_sha256']}")
    print("未创建任何视觉任务、结果或事件；本脚本只写模型登记行。")
    print_license_notice()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
