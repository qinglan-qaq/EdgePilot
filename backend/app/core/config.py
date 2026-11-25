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


@lru_cache
def get_settings() -> Settings:
    return Settings()
