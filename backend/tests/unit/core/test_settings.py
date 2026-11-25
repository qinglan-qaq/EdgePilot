from app.core.config import Settings


def test_settings_have_safe_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.env == "dev"
    assert s.tenant_default_id == "00000000-0000-0000-0000-000000000001"
    assert s.obs_enabled is False
    assert s.obs_capture_mode == "REDACTED"
    assert s.obs_queue_size == 10000
    assert s.obs_batch_size == 200
    assert s.access_token_minutes == 15
    assert s.refresh_token_days == 7


def test_database_url_comes_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    s = Settings(_env_file=None)
    assert s.database_url.endswith("/db")
