"""视觉事件入库服务。

纯服务函数：接收一帧（已由 detector 产出 detections）→ 规则引擎候选 → 可选
抓图 → 写 ``vision_events``。``dedup_key`` 冲突按幂等跳过，返回本次真正新建
的事件；不编造缺失字段、不接异步队列、不依赖 edge-agent。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.event import VisionEvent
from app.vision.detector import Detector
from app.vision.rules import Frame, RuleEngine
from app.vision.snapshot import FfmpegRunner, SnapshotResult, capture_snapshot

logger = logging.getLogger(__name__)

SnapshotSource = str | dict[Any, str] | Callable[[int], str | None]


def _resolve_uri(
    source: SnapshotSource | None, device_id: int
) -> str | None:
    if source is None:
        return None
    if callable(source):
        uri = source(device_id)
    elif isinstance(source, dict):
        uri = source.get(device_id) or source.get(str(device_id))
    elif isinstance(source, str):
        uri = source
    else:
        raise ValueError("snapshot_source 必须是 str / dict / callable")

    if not isinstance(uri, str) or not uri.strip():
        return None
    return uri.strip()


def _to_decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(float(value)))


async def ingest_frame(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    frame: Frame,
    detector: Detector,
    rules: RuleEngine,
    *,
    snapshot_source: SnapshotSource | None = None,
    storage_dir: str | Path | None = None,
    snapshot_runner: FfmpegRunner | None = None,
) -> list[VisionEvent]:
    """把一帧的规则事件写入 ``vision_events``。

    - ``frame.detections`` 必须由调用方用 ``detector.detect`` 产出，服务不再重跑检测；
    - ``detector`` 只用于解析 ``model_version`` 与 mock 标记；
    - ``snapshot_source`` 可为固定 URI、``{device_id: uri}`` 或 ``callable(device_id)``；
    - 同一 ``(tenant_id, dedup_key)`` 已存在时跳过该候选。
    """
    root = Path(storage_dir) if storage_dir is not None else Path(get_settings().storage_dir)
    model_version = getattr(detector, "model_version", None)
    is_mock = bool(getattr(detector, "is_mock", False)) or model_version == "mock-v1"

    created: list[VisionEvent] = []
    for candidate in rules.process(frame):
        snapshot: SnapshotResult | None = None
        uri = _resolve_uri(snapshot_source, candidate.device_id)
        if uri is not None:
            snapshot = capture_snapshot(
                uri,
                candidate.occurred_at,
                root,
                ffmpeg_runner=snapshot_runner,
                device_id=candidate.device_id,
            )

        payload = dict(candidate.payload or {})
        if is_mock:
            # 显式标注模拟数据来源，后续查询/Agent 可据此识别，不冒充真实推理。
            payload.setdefault("simulated", True)
            payload.setdefault("mock", True)
        if snapshot is not None:
            payload["snapshot_degraded"] = snapshot.degraded
            if snapshot.degraded and snapshot.error:
                payload["snapshot_error"] = snapshot.error

        row = VisionEvent(
            tenant_id=tenant_id,
            node_id=frame.node_id,
            device_id=candidate.device_id,
            event_type=candidate.event_type,
            severity=candidate.severity,
            occurred_at=candidate.occurred_at,
            confidence=_to_decimal(candidate.confidence),
            track_id=candidate.track_id,
            snapshot_path=snapshot.path if snapshot is not None else None,
            bbox=candidate.bbox,
            model_version=model_version,
            payload=payload,
            dedup_key=candidate.dedup_key,
            status="new",
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            logger.info("视觉事件 dedup_key 已存在，跳过 key=%s", candidate.dedup_key)
            continue
        created.append(row)

    await session.commit()
    for row in created:
        await session.refresh(row)
    return created
