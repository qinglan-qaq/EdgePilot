from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "edgepilot"
    env: str = "dev"
    # 本机 5432 已被本地 PostgreSQL 占用；docker compose 里的 PG 映射到 5433
    database_url: str = "postgresql+asyncpg://edgepilot:edgepilot@localhost:5433/edgepilot"
    test_database_url: str = "postgresql+asyncpg://edgepilot:edgepilot@localhost:5433/edgepilot_test"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://edgepilot:edgepilot@localhost:5672//"
    # Celery：broker 留空时回退 rabbitmq_url；result backend 留空表示不取结果。
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    # 测试夹具会关闭；生产开启但 enqueue 失败只告警，绝不影响 API。
    celery_enabled: bool = True
    workflow_stale_after_s: int = 60
    workflow_scan_interval_s: int = 60
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    # 默认关闭：单元/集成测试与本地 API 不因 broker 缺失而受影响。
    mqtt_enabled: bool = False
    mqtt_dedupe_ttl_seconds: int = 86400
    node_offline_after_s: float = 15.0
    node_sweep_interval_s: float = 5.0

    jwt_secret: str = "dev-only-change-me"
    # 独立的密钥加密密钥（Fernet）；生产必须由 SECRET_ENCRYPTION_KEY 提供。
    # 留空时仅开发环境由 jwt_secret 派生稳定密钥，见 app/core/crypto.py。
    secret_encryption_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    tenant_default_id: str = "00000000-0000-0000-0000-000000000001"

    obs_enabled: bool = False
    obs_capture_mode: str = "REDACTED"  # OFF | METADATA | REDACTED | FULL
    obs_queue_size: int = 10000
    obs_batch_size: int = 200
    obs_flush_ms: int = 500
    obs_sample_rate: float = 1.0
    obs_live_ttl_seconds: int = 3600

    llm_mode: str = "auto"  # real | mock | auto
    llm_fixture_dir: str = "tests/fixtures/llm"
    deepseek_api_key: str = ""
    storage_dir: str = "./var"
    snapshot_ttl_days: int = 90
    metrics_enabled: bool = True

    # ---------------------------------------------------------------- Vision Center
    # 推理服务不可用不影响既有模块；关闭后 vision 路由 503、轮询器不启动。
    vision_enabled: bool = True
    vision_service_url: str = "http://127.0.0.1:8001"
    vision_service_token: str = ""
    vision_service_timeout_s: float = 5.0
    vision_poll_interval_s: float = 2.0
    vision_poll_max_failures: int = 30
    vision_poll_backoff_max_s: int = 30
    vision_task_timeout_s: int = 1800
    vision_storage_dir: str = ""
    vision_max_upload_mb: int = 200
    vision_max_video_seconds: int = 900
    vision_target_frames: int = 900
    vision_max_samples: int = 2000
    vision_max_frame_stride: int = 300
    vision_default_conf: float = 0.25
    vision_event_conf: float = 0.6
    vision_event_cooldown_s: int = 5
    vision_trigger_enabled: bool = True
    vision_trigger_workflow_name: str = "视觉事件研判（演示）"
    vision_agent_enabled: bool = True
    vision_agent_once_per_task: bool = True
    vision_max_retries: int = 3
    vision_result_retention_days: int = 90
    vision_model_key: str = "yolo11n"
    vision_media_ttl_s: int = 3600

    @property
    def vision_storage_root(self):
        from pathlib import Path

        if self.vision_storage_dir:
            return Path(self.vision_storage_dir)
        return Path(self.storage_dir) / "vision"


@lru_cache
def get_settings() -> Settings:
    return Settings()
