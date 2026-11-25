"""EdgePilot 演示种子数据（P5 Task 20）。

从 ``backend`` 目录运行：

    python scripts/seed_demo.py

生成内容全部明确标记为演示/模拟，不冒充真实设备读数：

- 2 个节点（``demo-node-1/2``）、4 台演示相机；
- 恰好 200 条近 24h 的 ``vision_events``，区域分布 90/60/35/15；
- 20 张 Pillow 生成的占位截图（带 DEMO 字样），只挂到 20 条事件；
- 3 篇中文知识文档，走真实 RAG 入库；无 embedding 时 embedding=NULL，但
  pg_trgm/全文词面检索可命中。

幂等：重复运行不增加节点/设备/事件/文档/分块。脚本会清理 P4 遗留的
``sim-node-*`` 模拟事件（dedup_key 前缀识别，仅演示数据），以保证事件总数为 200。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 允许 `python scripts/seed_demo.py` 直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.tenancy import reset_tenant_id, set_tenant_id  # noqa: E402
from app.db import get_session_factory  # noqa: E402
from app.models.device import Device, EdgeNode  # noqa: E402
from app.models.event import VisionEvent  # noqa: E402
from app.models.knowledge import KnowledgeChunk  # noqa: E402
from app.rag.service import RagService  # noqa: E402

DEMO_NODES = (
    {"node_key": "demo-node-1", "name": "演示边缘节点 1", "ip": "10.10.0.11"},
    {"node_key": "demo-node-2", "name": "演示边缘节点 2", "ip": "10.10.0.12"},
)

# (相机名, 节点索引, 区域, 事件数)
DEMO_CAMERAS = (
    ("演示相机-东门", 0, "东门大厅", 90),
    ("演示相机-仓库", 1, "仓库通道", 60),
    ("演示相机-楼梯", 0, "二号楼梯", 35),
    ("演示相机-装卸", 1, "装卸区", 15),
)

EVENT_TYPES = ("zone", "line", "dwell", "count", "anomaly")
SEVERITIES = ("low", "medium", "high", "high")
STATUSES = ("new", "new", "acked", "ignored")
SNAPSHOT_COUNT = 20


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _demo_png(path: Path, index: int) -> None:
    """Pillow 演示占位图：纯色背景 + 明显 DEMO 文字，不含任何检测框。"""
    from PIL import Image, ImageDraw

    palette = [
        (44, 62, 80),
        (52, 73, 94),
        (39, 60, 117),
        (60, 99, 130),
        (80, 80, 120),
    ]
    image = Image.new("RGB", (480, 270), palette[index % len(palette)])
    draw = ImageDraw.Draw(image)
    draw.text((16, 16), "DEMO SNAPSHOT", fill=(255, 255, 255))
    draw.text((16, 44), f"simulated placeholder #{index + 1:02d}", fill=(230, 230, 230))
    draw.text((16, 84), "NOT A REAL DETECTION", fill=(255, 210, 120))
    draw.rectangle((16, 200, 464, 254), outline=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG")


def _doc_contents() -> list[tuple[str, str, str]]:
    """(文件名, 标题, 正文)。正文长度足够产生多个 400–600 字符分块。"""
    fire = """# 消防通道管理规范

消防通道是火灾等紧急情况下人员疏散和消防车辆通行的生命通道，任何部门和个人不得占用、堵塞或封闭消防通道。日常巡检发现物品堆放、车辆违停、临时施工围挡等情况，应立即通知责任人清理，并在巡检记录中登记位置、时间与处理结果。消防通道地面标识线应保持清晰，疏散指示牌与应急照明需定期测试，确保断电情况下仍可辨认方向。

## 巡检频次与责任

仓库、生产车间、办公区的主要消防通道每日至少巡检一次，节假日和大型活动前增加一次专项检查。巡检人员发现消防通道宽度不足、防火门无法完全开启、安全出口被锁闭等问题，应第一时间上报值班主管。涉及电气线路、易燃物堆放等重大隐患时，应同步通知安全管理部门并留存现场照片，照片仅作为整改证据，不得伪造或重复使用。

## 处置流程

发现占用消防通道的行为后，先现场劝阻并记录车牌、货物或施工信息；劝阻无效时提交工单，由安全管理部门协调清理。整改完成后需复查并关闭工单，复查内容包括通道净宽、地面标线、应急照明和疏散指示。所有工单应在系统中保留完整时间线，便于追溯责任和统计高频问题区域。
"""

    crowd = """# 人群聚集风险处置指引

人群聚集是指单位面积内人员密度快速上升、通行速度明显下降的状态，常见于出入口、促销活动、演出散场和公共交通接驳区域。监控发现人群聚集趋势时，值班人员应结合现场画面判断是否存在推挤、逆行或跌倒风险，必要时启动分流预案，通过广播、隔离带和单向通行等方式降低密度。

## 预警分级

人群聚集预警分为关注、警告和紧急三级。关注级表示局部密度升高但仍可通行，应加强观察并准备分流物资；警告级表示出现明显拥堵或通行停滞，应增派人员现场引导并限制后续人员进入；紧急级表示出现跌倒、踩踏或人员受伤风险，应立即启动应急响应，通知医疗和安保力量，并保留完整的视频与处置记录。

## 现场处置要点

现场处置坚持先控入口、再疏出口的原则，避免双向人流对冲。广播内容应简短明确，指引人员向空闲通道移动；隔离设施应留出应急通道，不得封死全部出口。处置结束后需复盘峰值人数、持续时间和处置动作，形成改进建议。系统记录中的模拟数据必须标注 simulated 标记，不得当作真实检测结论对外发布。
"""

    inspect = """# 设备巡检与异常上报

设备巡检覆盖边缘节点、摄像头、网络链路和供电设施。巡检人员应按计划检查设备在线状态、镜头遮挡、存储空间、温度与风扇运行情况，并核对最近一次心跳时间。发现摄像头画面模糊、偏转、离线或存储写入失败时，应立即登记设备编号、安装位置和故障现象，避免只写“异常”而缺少可定位信息。

## 巡检记录要求

巡检记录必须真实、完整、可追溯，包含巡检时间、设备编号、检查项目、结果和处置动作。对于无法现场修复的问题，应创建维修工单并注明影响范围。演示环境中的巡检数据需要明确标注为模拟数据，禁止把演示截图或模拟读数当作真实设备状态提交给运维值班。

## 异常升级

同一设备连续多次离线、关键区域摄像头失效或边缘节点磁盘空间不足时，应升级为高优先级工单并通知值班主管。升级后需持续跟踪处理进度，直到设备恢复在线并通过画面与存储校验。所有升级与恢复动作应记录在审计日志中，便于后续统计设备可用率和平均修复时间。
"""
    return [
        ("demo-fire-safety.md", "演示文档：消防通道管理规范", fire),
        ("demo-crowd-control.md", "演示文档：人群聚集风险处置指引", crowd),
        ("demo-device-inspection.md", "演示文档：设备巡检与异常上报", inspect),
    ]


async def _ensure_nodes(session: AsyncSession, tenant_id: uuid.UUID) -> list[EdgeNode]:
    nodes: list[EdgeNode] = []
    for spec in DEMO_NODES:
        node = await session.scalar(
            select(EdgeNode).where(
                EdgeNode.tenant_id == tenant_id,
                EdgeNode.node_key == spec["node_key"],
            )
        )
        if node is None:
            node = EdgeNode(
                tenant_id=tenant_id,
                node_key=spec["node_key"],
                name=spec["name"],
                os="linux",
                arch="x86_64",
                agent_version="demo-seed",
                status="online",
                ip=spec["ip"],
                labels={"simulated": True, "seed": "demo"},
            )
            session.add(node)
            await session.flush()
        nodes.append(node)
    return nodes


async def _ensure_devices(
    session: AsyncSession, tenant_id: uuid.UUID, nodes: list[EdgeNode]
) -> list[Device]:
    devices: list[Device] = []
    for index, (name, node_index, zone, _count) in enumerate(DEMO_CAMERAS):
        device = await session.scalar(
            select(Device).where(
                Device.tenant_id == tenant_id,
                Device.node_id == nodes[node_index].id,
                Device.name == name,
            )
        )
        if device is None:
            device = Device(
                tenant_id=tenant_id,
                node_id=nodes[node_index].id,
                name=name,
                kind="camera",
                config={
                    "simulated": True,
                    "seed": f"demo-camera-{index + 1}",
                    "zone": zone,
                },
                status="online",
            )
            session.add(device)
            await session.flush()
        devices.append(device)
    return devices


async def _cleanup_legacy_demo_events(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """删除 P4 遗留的 sim-node-* 模拟事件，避免演示总数被历史模拟数据污染。"""
    result = await session.execute(
        delete(VisionEvent).where(
            VisionEvent.tenant_id == tenant_id,
            VisionEvent.dedup_key.like("sim-node-%"),
        )
    )
    return int(result.rowcount or 0)


async def _ensure_events(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    nodes: list[EdgeNode],
    devices: list[Device],
    *,
    now: datetime,
    snapshot_dir: Path,
) -> tuple[int, int]:
    """确保 200 条演示事件；返回 (新建事件数, 本次补写的快照文件数)。"""
    keys = [
        f"seed:demo:{zone_index}:{index:03d}"
        for zone_index, (_name, _node, _zone, count) in enumerate(DEMO_CAMERAS)
        for index in range(count)
    ]
    existing_rows = (
        await session.execute(
            select(VisionEvent.dedup_key, VisionEvent.snapshot_path).where(
                VisionEvent.tenant_id == tenant_id,
                VisionEvent.dedup_key.in_(keys),
            )
        )
    ).all()
    existing = {row.dedup_key: row.snapshot_path for row in existing_rows}

    created = 0
    snapshots_written = 0
    global_index = 0
    for zone_index, (_name, node_index, zone, count) in enumerate(DEMO_CAMERAS):
        device = devices[zone_index]
        for index in range(count):
            dedup_key = f"seed:demo:{zone_index}:{index:03d}"
            occurred_at = now - timedelta(minutes=(global_index * 7 + zone_index * 13) % 1439)
            is_new = dedup_key not in existing
            snapshot_path = None if is_new else existing[dedup_key]
            if global_index < SNAPSHOT_COUNT:
                if is_new:
                    day = occurred_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
                    relative = (
                        Path("snapshots") / "events" / day / f"demo-{global_index:02d}.png"
                    )
                    _demo_png(snapshot_dir / relative, global_index)
                    snapshot_path = relative.as_posix()
                    snapshots_written += 1
                elif snapshot_path:
                    # 已有事件只补缺失文件，沿用库内路径，避免跨天重跑堆出新目录。
                    relative = Path(snapshot_path)
                    if not relative.is_absolute() and ".." not in relative.parts:
                        target = snapshot_dir / relative
                        if not target.is_file():
                            _demo_png(target, global_index)
                            snapshots_written += 1

            if is_new:
                session.add(
                    VisionEvent(
                        tenant_id=tenant_id,
                        node_id=nodes[node_index].id,
                        device_id=device.id,
                        event_type=EVENT_TYPES[global_index % len(EVENT_TYPES)],
                        severity=SEVERITIES[global_index % len(SEVERITIES)],
                        occurred_at=occurred_at,
                        confidence=None,  # 不伪造检测置信度
                        track_id=f"demo-track-{global_index:03d}",
                        snapshot_path=snapshot_path,
                        bbox=None,
                        model_version="demo-seed-v1",
                        payload={
                            "simulated": True,
                            "seed": "demo",
                            "zone": zone,
                            "camera": device.name,
                            "node_key": nodes[node_index].node_key,
                            "note": "演示数据，非真实设备检测结果",
                        },
                        dedup_key=dedup_key,
                        status=STATUSES[global_index % len(STATUSES)],
                    )
                )
                created += 1
            global_index += 1
    await session.flush()
    return created, snapshots_written


async def _ensure_documents(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[int, int, int]:
    """确保 3 篇演示文档；返回 (文档数, 分块数, 新建文档数)。"""
    rag = RagService(session)
    created = 0
    doc_ids: list[str] = []
    for filename, title, content in _doc_contents():
        result = await rag.ingest_text(
            tenant_id=tenant_id,
            filename=filename,
            text=content,
            title=title,
            commit=False,
        )
        doc_ids.append(result.doc_id)
        if result.created:
            created += 1
    await session.flush()
    chunk_count = int(
        await session.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.doc_id.in_(doc_ids),
            )
        )
        or 0
    )
    return len(doc_ids), chunk_count, created


async def seed_all(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    storage_dir: str | Path,
    now: datetime | None = None,
    cleanup_legacy_demo: bool = True,
) -> dict:
    """在给定 session 内执行完整种子；不 commit，便于测试事务回滚。"""
    when = now or _now_utc()
    root = Path(storage_dir)
    if not root.is_absolute():
        root = Path.cwd() / root

    legacy_removed = (
        await _cleanup_legacy_demo_events(session, tenant_id)
        if cleanup_legacy_demo
        else 0
    )
    nodes = await _ensure_nodes(session, tenant_id)
    devices = await _ensure_devices(session, tenant_id, nodes)
    events_created, snapshots = await _ensure_events(
        session, tenant_id, nodes, devices, now=when, snapshot_dir=root
    )
    docs, chunks, docs_created = await _ensure_documents(session, tenant_id)
    await session.flush()

    events_total = int(
        await session.scalar(
            select(func.count())
            .select_from(VisionEvent)
            .where(VisionEvent.tenant_id == tenant_id)
        )
        or 0
    )
    seed_events = int(
        await session.scalar(
            select(func.count())
            .select_from(VisionEvent)
            .where(
                VisionEvent.tenant_id == tenant_id,
                VisionEvent.dedup_key.like("seed:demo:%"),
            )
        )
        or 0
    )
    snapshot_paths = int(
        await session.scalar(
            select(func.count())
            .select_from(VisionEvent)
            .where(
                VisionEvent.tenant_id == tenant_id,
                VisionEvent.dedup_key.like("seed:demo:%"),
                VisionEvent.snapshot_path.isnot(None),
            )
        )
        or 0
    )

    return {
        "tenant_id": str(tenant_id),
        "nodes": len(nodes),
        "devices": len(devices),
        "events_total": events_total,
        "seed_events": seed_events,
        "events_created": events_created,
        "snapshot_paths": snapshot_paths,
        "snapshots_written": snapshots,
        "docs": docs,
        "docs_created": docs_created,
        "chunks": chunks,
        "legacy_demo_events_removed": legacy_removed,
        "storage_dir": str(root),
    }


async def main() -> dict:
    settings = get_settings()
    tenant_id = uuid.UUID(settings.tenant_default_id)
    token = set_tenant_id(tenant_id)
    try:
        async with get_session_factory()() as session:
            stats = await seed_all(
                session,
                tenant_id=tenant_id,
                storage_dir=settings.storage_dir,
            )
            await session.commit()
    finally:
        reset_tenant_id(token)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return stats


if __name__ == "__main__":
    asyncio.run(main())
