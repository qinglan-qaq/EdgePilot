"""截图与占位图降级。

真实截图只调用外部 ``ffmpeg``；失败（二进制缺失 / 超时 / 非零退出）时用 Pillow
生成纯色占位图，并保留真实错误原因。占位图不含任何伪造检测框，禁止拿别的
图片冒充。输出路径完全由受控目录 + uuid 生成，用户 URI 只作为 ffmpeg 输入。
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

FfmpegRunner = Callable[[list[str]], None]

_PLACEHOLDER_SIZE = (320, 180)
_PLACEHOLDER_COLOR = (96, 96, 96)


@dataclass(frozen=True)
class SnapshotResult:
    path: str | None
    degraded: bool
    error: str | None = None


def _run_ffmpeg(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)


def _safe_device_name(device_id: object) -> str:
    text = "".join(
        char if (char.isalnum() or char in "-_") else "_" for char in str(device_id)
    )[:32]
    return text or "device"


def _write_placeholder(path: Path) -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", _PLACEHOLDER_SIZE, _PLACEHOLDER_COLOR)
    draw = ImageDraw.Draw(image)
    draw.text((10, 10), "snapshot unavailable", fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=80)


def capture_snapshot(
    uri: str,
    occurred_at: datetime,
    output_dir: str | Path,
    *,
    ffmpeg_runner: FfmpegRunner | None = None,
    device_id: object = "device",
    seek_seconds: float = 0.0,
) -> SnapshotResult:
    """抓取 ``uri`` 在 ``seek_seconds`` 处的一帧。

    ``occurred_at`` 只用于按日期分目录；``seek_seconds`` 是显式的抽帧位置。
    """
    when = occurred_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    day = when.strftime("%Y-%m-%d")

    root = Path(output_dir)
    relative = (
        Path("snapshots")
        / "events"
        / day
        / f"{_safe_device_name(device_id)}-{uuid.uuid4().hex}.jpg"
    )
    output_path = root / relative
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "ffmpeg",
        "-ss",
        f"{max(0.0, float(seek_seconds)):g}",
        "-i",
        str(uri),
        "-frames:v",
        "1",
        "-y",
        str(output_path),
    ]
    runner = ffmpeg_runner or _run_ffmpeg

    try:
        runner(command)
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("ffmpeg 未生成输出文件")
    except Exception as exc:  # noqa: BLE001 - 任何抓图失败都降级
        error = str(exc) or type(exc).__name__
        logger.warning("截图失败，使用占位图 uri=%s error=%s", uri, error)
        try:
            _write_placeholder(output_path)
        except Exception as placeholder_exc:  # noqa: BLE001
            return SnapshotResult(
                path=None,
                degraded=True,
                error=f"{error}; 占位图生成失败: {placeholder_exc}",
            )
        return SnapshotResult(path=relative.as_posix(), degraded=True, error=error)

    return SnapshotResult(path=relative.as_posix(), degraded=False, error=None)


def resolve_snapshot_path(storage_dir: str | Path, snapshot_path: str) -> Path:
    """把库内相对路径解析为 storage_dir 下的绝对路径；越界一律 ValueError。"""
    if not isinstance(snapshot_path, str) or not snapshot_path.strip():
        raise ValueError("snapshot_path 为空")
    if "\x00" in snapshot_path:
        raise ValueError("snapshot_path 含非法字符")

    root = Path(storage_dir).resolve()
    relative = Path(snapshot_path)
    if relative.is_absolute() or relative.drive:
        raise ValueError("snapshot_path 不允许绝对路径")

    candidate = (root / relative).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        raise ValueError("snapshot_path 越出存储目录")
    return candidate
