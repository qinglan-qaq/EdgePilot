from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "edgepilot"
    env: str = "dev"
    database_url: str = "postgresql+asyncpg://edgepilot:edgepilot@localhost:5432/edgepilot"
    test_database_url: str = "postgresql+asyncpg://edgepilot:edgepilot@localhost:5432/edgepilot_test"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://edgepilot:edgepilot@localhost:5672//"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""

    jwt_secret: str = "dev-only-change-me"
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
