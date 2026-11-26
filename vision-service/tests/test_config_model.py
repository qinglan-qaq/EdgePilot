from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.model import ModelRegistry


def test_default_limits():
    settings = Settings()
    assert settings.max_file_bytes == 200 * 1024 * 1024
    assert settings.max_video_seconds == 900
    assert settings.target_samples == 900
    assert settings.max_samples == 2000
    assert settings.job_timeout_s == 1800


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("VISION_MAX_FILE_MB", "10")
    monkeypatch.setenv("VISION_MAX_SAMPLES", "1500")
    settings = Settings.from_env()
    assert settings.max_file_bytes == 10 * 1024 * 1024
    assert settings.max_samples == 1500


def test_missing_weights_is_honest(tmp_path: Path):
    registry = ModelRegistry(Settings(model_path=tmp_path / "none.pt", preload=False))
    info = registry.info()
    assert info["load_status"] == "missing_weights"
    assert info["status"] == "missing_weights"
    assert info["license"] == "AGPL-3.0"
    assert "商业" in info["commercial_use_notice"]
    assert info["model_version"] is None


def test_storage_writable(tmp_path: Path):
    settings = Settings(jobs_dir=tmp_path / "jobs")
    assert settings.storage_writable() is True
    assert (tmp_path / "jobs" / ".health-write").exists() is False


def test_model_version_uses_weight_sha(tmp_path: Path):
    weights = tmp_path / "yolo11n.pt"
    weights.write_bytes(b"fake-weights")
    registry = ModelRegistry(Settings(model_path=weights, preload=False))
    info = registry.info()
    assert info["model_version"].startswith("yolo11n-")
    assert len(info["model_version"].split("-")[1]) == 12
    assert len(info["weights_sha256"]) == 64
