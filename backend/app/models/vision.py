from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin


class VisionTask(TenantMixin, TimestampMixin, Base):
    """一次视频识别任务的汇总行；帧级结果在 artifact 文件里，不入库。"""

    __tablename__ = "vision_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_uid", name="uq_vision_tasks_tenant_uid"),
        UniqueConstraint("tenant_id", "dedup_key", name="uq_vision_tasks_tenant_dedup"),
        Index("ix_vision_tasks_tenant_created", "tenant_id", text("created_at DESC")),
        Index("ix_vision_tasks_tenant_status", "tenant_id", "status"),
        Index("ix_vision_tasks_tenant_model", "tenant_id", "model_name"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_uid: Mapped[str] = mapped_column(String(36), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="upload")
    video_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    video_path: Mapped[str] = mapped_column(String(512), nullable=False)
    video_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(64))
    conf_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, server_default="0.250"
    )
    frame_stride: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    target_samples: Mapped[int] = mapped_column(Integer, nullable=False, server_default="900")
    zones: Mapped[list | None] = mapped_column(JSONB)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed_frames: Mapped[int | None] = mapped_column(Integer)
    detection_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    inference_ms: Mapped[int | None] = mapped_column(Integer)
    video_fps: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    video_duration_ms: Mapped[int | None] = mapped_column(Integer)
    video_width: Mapped[int | None] = mapped_column(Integer)
    video_height: Mapped[int | None] = mapped_column(Integer)
    frame_count: Mapped[int | None] = mapped_column(Integer)
    sampled_every_n_frames: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(512))
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    poll_misses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)


class VisionResult(TenantMixin, TimestampMixin, Base):
    """任务结果汇总与 artifact 指针；一行对应一个任务。"""

    __tablename__ = "vision_results"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_vision_results_tenant_task"),
        Index("ix_vision_results_tenant_created", "tenant_id", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 与 task_id 双写：结果服务/事件/Agent 都按 task_uid 跨表关联，排障时也无需再查任务表。
    task_uid: Mapped[str] = mapped_column(String(36), nullable=False)
    # artifact_path 可空：清理流程保留汇总行但把文件指针置空（spec 7.4）。
    artifact_path: Mapped[str | None] = mapped_column(String(512))
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_file_sha256: Mapped[str | None] = mapped_column(String(64))
    # canonical JSON 的 SHA-256，与 artifact_file_sha256（文件字节）严格区分。
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result_schema_version: Mapped[str | None] = mapped_column(String(32))
    frame_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    class_counts: Mapped[dict | None] = mapped_column(JSONB)
    peak_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    inference_ms: Mapped[int | None] = mapped_column(Integer)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    summary: Mapped[dict | None] = mapped_column(JSONB)
    workflow_run_id: Mapped[int | None] = mapped_column(BigInteger)
    trigger_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    trigger_error: Mapped[str | None] = mapped_column(String(255))
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger)
    # 本次研判使用的事件（同任务多条事件时记录选中的那一条）。
    agent_event_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    agent_error: Mapped[str | None] = mapped_column(String(512))
    agent_trace_id: Mapped[str | None] = mapped_column(String(32))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VisionModel(TenantMixin, TimestampMixin, Base):
    """模型登记与许可提示；不存权重文件本身，字段按 spec 7.3。"""

    __tablename__ = "vision_models"
    __table_args__ = (
        UniqueConstraint("tenant_id", "model_key", name="uq_vision_models_tenant_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    model_key: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    weights_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    weights_sha256: Mapped[str | None] = mapped_column(String(64))
    task_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="detection")
    classes: Mapped[list | None] = mapped_column(JSONB)
    input_size: Mapped[int | None] = mapped_column(Integer)
    license: Mapped[str] = mapped_column(String(64), nullable=False, server_default="AGPL-3.0")
    license_url: Mapped[str | None] = mapped_column(String(255))
    commercial_use_notice: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="available")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
