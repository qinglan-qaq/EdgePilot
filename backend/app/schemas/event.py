"""视觉事件 API 响应模型。

``has_snapshot`` 表示事件是否登记了快照路径（文件是否存在由快照端点在读取时
校验，避免列表查询触盘）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int | None
    device_id: int | None
    event_type: str
    severity: str | None
    occurred_at: datetime
    received_at: datetime | None
    confidence: float | None
    track_id: str | None
    snapshot_path: str | None
    has_snapshot: bool = False
    bbox: dict | None
    model_version: str | None
    payload: dict | None
    dedup_key: str
    status: str
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row) -> "EventOut":
        return cls(
            id=row.id,
            node_id=row.node_id,
            device_id=row.device_id,
            event_type=row.event_type,
            severity=row.severity,
            occurred_at=row.occurred_at,
            received_at=row.received_at,
            confidence=float(row.confidence) if row.confidence is not None else None,
            track_id=row.track_id,
            snapshot_path=row.snapshot_path,
            has_snapshot=bool(row.snapshot_path),
            bbox=row.bbox,
            model_version=row.model_version,
            payload=row.payload,
            dedup_key=row.dedup_key,
            status=row.status,
            created_at=row.created_at,
        )
