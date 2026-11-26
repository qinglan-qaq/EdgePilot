from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent

LICENSE_NAME = "AGPL-3.0"
LICENSE_URL = "https://www.ultralytics.com/license"
COMMERCIAL_NOTICE = (
    "Ultralytics YOLO 采用 AGPL-3.0 许可。将本服务用于闭源商业产品或对外提供"
    "网络服务前，请自行评估 AGPL 义务或购买 Ultralytics 商业许可；本仓库不默认"
    "授予商业使用权限。"
)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{name} must be a number, got {value!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_roots(name: str) -> tuple[Path, ...]:
    raw = os.environ.get(name, "")
    if not raw:
        return ()
    parts = [p for p in raw.replace(";", os.pathsep).split(os.pathsep) if p.strip()]
    return tuple(Path(p).expanduser().resolve() for p in parts)


@dataclass(frozen=True)
class Settings:
    service_name: str = "edgepilot-vision"
    host: str = "0.0.0.0"
    port: int = 8001
    jobs_dir: Path = SERVICE_ROOT / "var" / "jobs"
    model_path: Path = SERVICE_ROOT / "weights" / "yolo11n.pt"
    model_key: str = "yolo11n"
    device: str = "cpu"
    imgsz: int = 640
    iou: float = 0.45
    max_det: int = 100
    default_conf: float = 0.25
    token: str = ""
    preload: bool = True
    max_workers: int = 1
    max_pending: int = 32
    job_timeout_s: int = 1800
    max_file_bytes: int = 200 * 1024 * 1024
    max_video_seconds: int = 900
    max_samples: int = 2000
    target_samples: int = 900
    allowed_roots: tuple[Path, ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()
        roots = _env_roots("VISION_MEDIA_ROOTS")
        return replace(
            defaults,
            host=_env_str("VISION_HOST", defaults.host),
            port=_env_int("VISION_PORT", defaults.port),
            jobs_dir=Path(_env_str("VISION_JOBS_DIR", str(defaults.jobs_dir))),
            model_path=Path(_env_str("VISION_MODEL_PATH", str(defaults.model_path))),
            model_key=_env_str("VISION_MODEL_KEY", defaults.model_key),
            device=_env_str("VISION_DEVICE", defaults.device),
            imgsz=_env_int("VISION_IMGSZ", defaults.imgsz),
            iou=_env_float("VISION_IOU", defaults.iou),
            max_det=_env_int("VISION_MAX_DET", defaults.max_det),
            default_conf=_env_float("VISION_DEFAULT_CONF", defaults.default_conf),
            token=_env_str("VISION_SERVICE_TOKEN", defaults.token),
            preload=_env_bool("VISION_PRELOAD", defaults.preload),
            max_workers=_env_int("VISION_MAX_WORKERS", defaults.max_workers),
            max_pending=_env_int("VISION_MAX_PENDING", defaults.max_pending),
            job_timeout_s=_env_int("VISION_JOB_TIMEOUT_S", defaults.job_timeout_s),
            max_file_bytes=_env_int("VISION_MAX_FILE_MB", 200) * 1024 * 1024,
            max_video_seconds=_env_int("VISION_MAX_VIDEO_SECONDS", defaults.max_video_seconds),
            max_samples=_env_int("VISION_MAX_SAMPLES", defaults.max_samples),
            target_samples=_env_int("VISION_TARGET_SAMPLES", defaults.target_samples),
            allowed_roots=roots,
        )

    def storage_writable(self) -> bool:
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            probe = self.jobs_dir / ".health-write"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False
