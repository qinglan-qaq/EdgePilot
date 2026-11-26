"""0006_vision: Vision Center 任务/结果/模型三张表

设计见 ``docs/specs/2026-11-26-edgepilot-vision-spec.md`` 第 7 节：
帧级检测结果存 JSON artifact 文件，数据库只保存任务汇总、artifact 指针与
联动关联字段。本迁移只新增三张表与索引，不改动任何既有表。

额外增加 ``uq_workflow_runs_vision_task``：同一 vision 任务只能有一条
``trigger_type='event'`` 的运行，作为任务级触发幂等的数据库兜底。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_vision"
down_revision = "0005_obs_analytics_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vision_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_uid", sa.String(36), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("source_type", sa.String(16), nullable=False, server_default="upload"),
        sa.Column("video_filename", sa.String(255), nullable=False),
        sa.Column("video_path", sa.String(512), nullable=False),
        sa.Column("video_sha256", sa.String(64), nullable=False),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.String(64)),
        sa.Column("conf_threshold", sa.Numeric(4, 3), nullable=False, server_default="0.250"),
        sa.Column("frame_stride", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("target_samples", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("zones", postgresql.JSONB()),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_frames", sa.Integer()),
        sa.Column("detection_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inference_ms", sa.Integer()),
        sa.Column("video_fps", sa.Numeric(7, 3)),
        sa.Column("video_duration_ms", sa.Integer()),
        sa.Column("video_width", sa.Integer()),
        sa.Column("video_height", sa.Integer()),
        sa.Column("frame_count", sa.Integer()),
        sa.Column("sampled_every_n_frames", sa.Integer()),
        sa.Column("model_name", sa.String(64)),
        sa.Column("model_version", sa.String(64)),
        sa.Column("device", sa.String(16)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.String(512)),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("poll_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("poll_misses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "task_uid", name="uq_vision_tasks_tenant_uid"),
        sa.UniqueConstraint("tenant_id", "dedup_key", name="uq_vision_tasks_tenant_dedup"),
    )
    op.create_index("ix_vision_tasks_tenant_id", "vision_tasks", ["tenant_id"])
    op.create_index(
        "ix_vision_tasks_tenant_created",
        "vision_tasks",
        ["tenant_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_vision_tasks_tenant_status", "vision_tasks", ["tenant_id", "status"])
    op.create_index(
        "ix_vision_tasks_tenant_model", "vision_tasks", ["tenant_id", "model_name"]
    )

    op.create_table(
        "vision_results",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=False),
        sa.Column("task_uid", sa.String(36), nullable=False),
        sa.Column("artifact_path", sa.String(512)),
        sa.Column("artifact_bytes", sa.BigInteger(), nullable=False),
        sa.Column("artifact_file_sha256", sa.String(64)),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("result_schema_version", sa.String(32)),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("detection_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("class_counts", postgresql.JSONB()),
        sa.Column("peak_confidence", sa.Numeric(4, 3)),
        sa.Column("inference_ms", sa.Integer()),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", postgresql.JSONB()),
        sa.Column("workflow_run_id", sa.BigInteger()),
        sa.Column("trigger_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("trigger_error", sa.String(255)),
        sa.Column("triggered_at", sa.DateTime(timezone=True)),
        sa.Column("agent_run_id", sa.BigInteger()),
        sa.Column("agent_event_id", sa.BigInteger()),
        sa.Column("agent_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("agent_error", sa.String(512)),
        sa.Column("agent_trace_id", sa.String(32)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_vision_results_tenant_task"),
    )
    op.create_index("ix_vision_results_tenant_id", "vision_results", ["tenant_id"])
    op.create_index(
        "ix_vision_results_tenant_created",
        "vision_results",
        ["tenant_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "vision_models",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_key", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model_version", sa.String(64), nullable=False),
        sa.Column("weights_filename", sa.String(255), nullable=False),
        sa.Column("weights_sha256", sa.String(64)),
        sa.Column("task_type", sa.String(16), nullable=False, server_default="detection"),
        sa.Column("classes", postgresql.JSONB()),
        sa.Column("input_size", sa.Integer()),
        sa.Column("license", sa.String(64), nullable=False, server_default="AGPL-3.0"),
        sa.Column("license_url", sa.String(255)),
        sa.Column("commercial_use_notice", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False, server_default="available"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "model_key", name="uq_vision_models_tenant_key"),
    )
    op.create_index("ix_vision_models_tenant_id", "vision_models", ["tenant_id"])

    op.execute(
        """
        CREATE UNIQUE INDEX uq_workflow_runs_vision_task
        ON workflow_runs ((trigger_payload->>'vision_task_uid'))
        WHERE trigger_type = 'event'
          AND trigger_payload->>'vision_task_uid' IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_workflow_runs_vision_task")
    # 用 IF EXISTS 兼容早期 0006 版本（未包含新增索引）的降级。
    op.execute("DROP INDEX IF EXISTS ix_vision_models_tenant_id")
    op.drop_table("vision_models")
    op.execute("DROP INDEX IF EXISTS ix_vision_results_tenant_created")
    op.execute("DROP INDEX IF EXISTS ix_vision_results_tenant_id")
    op.drop_table("vision_results")
    op.execute("DROP INDEX IF EXISTS ix_vision_tasks_tenant_model")
    op.execute("DROP INDEX IF EXISTS ix_vision_tasks_tenant_status")
    op.execute("DROP INDEX IF EXISTS ix_vision_tasks_tenant_created")
    op.execute("DROP INDEX IF EXISTS ix_vision_tasks_tenant_id")
    op.drop_table("vision_tasks")
