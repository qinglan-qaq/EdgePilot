from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class VisionZone(BaseModel):
    zone_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9._-]+$")
    name: str | None = Field(default=None, max_length=64)
    polygon: list[list[float]]

    @field_validator("polygon")
    @classmethod
    def _validate_polygon(cls, points: list[list[float]]) -> list[list[float]]:
        if not 3 <= len(points) <= 32:
            raise ValueError("区域至少 3 个点，最多 32 个点")
        for point in points:
            if len(point) != 2:
                raise ValueError("每个点必须是 [x, y]")
            if not all(0.0 <= float(value) <= 1.0 for value in point):
                raise ValueError("区域坐标必须是 0..1 的归一化值")
        return points


class VisionZonesIn(BaseModel):
    zones: list[VisionZone] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _unique_zone_ids(self) -> "VisionZonesIn":
        ids = [zone.zone_id for zone in self.zones]
        if len(ids) != len(set(ids)):
            raise ValueError("zone_id 必须唯一")
        return self


class VisionTaskOut(BaseModel):
    id: int
    task_uid: str
    status: str
    progress: int = 0
    # 派发/重试次数：retry 端点在响应里回显，前端据此展示重试余量。
    dispatch_attempts: int = 0
    video_filename: str
    file_size_bytes: int
    conf_threshold: float
    frame_stride: int
    target_samples: int
    video_fps: float | None = None
    video_duration_ms: int | None = None
    video_width: int | None = None
    video_height: int | None = None
    frame_count: int | None = None
    sampled_every_n_frames: int | None = None
    model_name: str | None = None
    model_version: str | None = None
    device: str | None = None
    processed_frames: int | None = None
    detection_count: int = 0
    # 列表/详情都直接展示事件数，取 vision_results.event_count。
    event_count: int = 0
    inference_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None


class VisionTaskListOut(BaseModel):
    items: list[VisionTaskOut]
    total: int
    page: int
    size: int


class VisionResultOut(BaseModel):
    task_uid: str
    frame_count: int | None = None
    detection_count: int = 0
    class_counts: dict[str, int] = Field(default_factory=dict)
    peak_confidence: float | None = None
    inference_ms: int | None = None
    event_count: int = 0
    summary: dict[str, Any] | None = None
    workflow_run_id: int | None = None
    trigger_status: str = "pending"
    trigger_error: str | None = None
    agent_run_id: int | None = None
    # 本次研判使用的事件；任务级触发时可能为空。
    agent_event_id: int | None = None
    agent_status: str = "none"
    agent_error: str | None = None
    agent_trace_id: str | None = None
    artifact_path: str | None = None
    artifact_bytes: int | None = None
    artifact_file_sha256: str | None = None
    result_sha256: str | None = None
    result_schema_version: str | None = None
    purged_at: datetime | None = None


class VisionTaskDetailOut(VisionTaskOut):
    """任务详情：在列表字段上补充视频可播放状态与结果汇总。"""

    has_video: bool = True
    video_missing: bool = False
    artifact_purged: bool = False
    video_url: str | None = None
    video_expires_at: datetime | None = None
    result: VisionResultOut | None = None


class VisionEventOut(BaseModel):
    id: int
    event_type: str
    severity: str | None = None
    confidence: float | None = None
    occurred_at: datetime
    bbox: dict[str, float] | None = None
    payload: dict[str, Any] | None = None
    dedup_key: str


class VisionOverviewOut(BaseModel):
    """统计卡片：按 spec §8.2 分组，前端直接按 tasks/frames/... 读取。"""

    tasks: dict[str, Any] = Field(default_factory=dict)
    frames: dict[str, Any] = Field(default_factory=dict)
    detections: dict[str, Any] = Field(default_factory=dict)
    events: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] | None = None
    recent_tasks: list[VisionTaskOut] = Field(default_factory=list)


class VisionHealthOut(BaseModel):
    """服务/模型健康状态；不可达时 HTTP 仍 200，只把 reachable 置 false。"""

    enabled: bool = True
    reachable: bool = False
    service: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    error: str | None = None
