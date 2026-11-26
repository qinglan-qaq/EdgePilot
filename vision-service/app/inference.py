from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import LICENSE_NAME, Settings
from .model import ModelRegistry, ModelUnavailable
from .standardize import build_artifact, clamp_bbox, make_detection, make_frame

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}


class InferenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class JobCancelled(RuntimeError):
    pass


@dataclass
class JobSpec:
    task_uid: str
    video_path: str
    filename: str = ""
    device_id: str | None = None
    video_sha256: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _cv2() -> Any:
    import cv2  # imported lazily so tests can inject a fake implementation

    return cv2


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        number = float(value)
        if not math.isfinite(number):
            return default
        return int(round(number))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def validate_video_file(path: Path, settings: Settings) -> int:
    if not path.is_file():
        raise InferenceError("video_missing", f"视频文件不存在: {path.name}")
    size = path.stat().st_size
    if size <= 0:
        raise InferenceError("empty_video", f"视频文件为空: {path.name}")
    if size > settings.max_file_bytes:
        raise InferenceError(
            "file_too_large",
            f"视频超过 {settings.max_file_bytes // (1024 * 1024)}MB 上限",
        )
    return size


def probe_video(
    path: str | Path, settings: Settings, registry: ModelRegistry | None = None
) -> dict[str, Any]:
    video_path = Path(path)
    size = validate_video_file(video_path, settings)
    cv2 = _cv2()
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise InferenceError("decode_failed", f"无法解码视频: {video_path.name}")
        width = _as_int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = _as_int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = _as_float(capture.get(cv2.CAP_PROP_FPS))
        reported = _as_int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        release = getattr(capture, "release", None)
        if callable(release):
            release()

    duration_ms: int | None = None
    if fps > 0 and reported > 0:
        duration_ms = int(round(reported * 1000.0 / fps))
    if duration_ms is not None and duration_ms > settings.max_video_seconds * 1000:
        raise InferenceError(
            "video_too_long",
            f"视频时长 {duration_ms / 1000:.1f}s 超过 {settings.max_video_seconds}s 上限",
        )

    model_info = registry.info() if registry is not None else {}
    return {
        "filename": video_path.name,
        "size_bytes": size,
        "width": width,
        "height": height,
        "fps": fps or None,
        "frame_count": reported,
        "duration_ms": duration_ms,
        "duration_known": duration_ms is not None,
        "max_video_seconds": settings.max_video_seconds,
        "max_samples": settings.max_samples,
        "target_samples": settings.target_samples,
        "model_name": model_info.get("model_name"),
        "model_version": model_info.get("model_version"),
        "weights_sha256": model_info.get("weights_sha256"),
    }


def _normalize_frame(frame: Any, width: int, height: int) -> tuple[Any, int, int]:
    shape = getattr(frame, "shape", None)
    if width <= 0 or height <= 0:
        if isinstance(shape, (tuple, list)) and len(shape) >= 2:
            height = height or int(shape[0])
            width = width or int(shape[1])
    return frame, max(1, width), max(1, height)


class YoloEngine:
    """Runs a video through YOLO and produces the standardized artifact.

    The engine reads timestamps from decoder PTS when available and only falls
    back to an FPS-based estimate when PTS is absent or non-monotonic. Sampling
    preserves the original ``frame_id`` so the frontend can match by playback
    time.
    """

    def __init__(self, registry: ModelRegistry, settings: Settings) -> None:
        self.registry = registry
        self.settings = settings

    def run(
        self,
        spec: JobSpec,
        progress: Callable[[int, dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        path = Path(spec.video_path)
        size = validate_video_file(path, self.settings)
        params = dict(spec.params or {})
        conf = float(params.get("conf_threshold", self.settings.default_conf))
        imgsz = int(params.get("imgsz") or self.settings.imgsz)
        iou = float(params.get("iou") or self.settings.iou)
        max_det = int(params.get("max_det") or self.settings.max_det)
        target_samples = int(params.get("target_samples") or self.settings.target_samples)
        requested_stride = max(1, min(int(params.get("frame_stride") or 1), 300))

        cv2 = _cv2()
        capture = cv2.VideoCapture(str(path))
        started = time.monotonic()
        deadline = started + self.settings.job_timeout_s

        frames: list[dict[str, Any]] = []
        processed = 0
        read_index = 0
        last_pts = -1.0
        pts_used = 0
        fallbacks = 0

        try:
            if not capture.isOpened():
                raise InferenceError("decode_failed", f"无法解码视频: {path.name}")
            width = _as_int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = _as_int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = _as_float(capture.get(cv2.CAP_PROP_FPS))
            reported = _as_int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            if fps > 0 and reported > 0:
                duration_ms: int | None = int(round(reported * 1000.0 / fps))
                if duration_ms > self.settings.max_video_seconds * 1000:
                    raise InferenceError(
                        "video_too_long",
                        f"视频时长 {duration_ms / 1000:.1f}s 超过 "
                        f"{self.settings.max_video_seconds}s 上限",
                    )
            else:
                duration_ms = None

            stride = requested_stride
            if reported > 0:
                stride = max(stride, math.ceil(reported / max(1, target_samples)))
            expected_samples = min(
                self.settings.max_samples,
                math.ceil(reported / stride) if reported > 0 else self.settings.max_samples,
            )

            while True:
                if cancelled is not None and cancelled():
                    raise JobCancelled()
                if time.monotonic() > deadline:
                    raise InferenceError(
                        "timeout", f"任务超过 {self.settings.job_timeout_s}s 未完成"
                    )
                ok, frame = capture.read()
                if not ok or frame is None:
                    break

                frame_index = read_index
                read_index += 1

                raw_pts = _as_float(capture.get(cv2.CAP_PROP_POS_MSEC), -1.0)
                if raw_pts >= 0 and raw_pts > last_pts:
                    timestamp_ms = int(round(raw_pts))
                    last_pts = raw_pts
                    pts_used += 1
                else:
                    timestamp_ms = (
                        int(round(frame_index * 1000.0 / fps)) if fps > 0 else frame_index * 40
                    )
                    fallbacks += 1

                if frame_index % stride != 0:
                    continue
                if processed >= self.settings.max_samples:
                    raise InferenceError(
                        "frame_limit_exceeded",
                        f"采样帧数超过硬上限 {self.settings.max_samples}",
                    )

                frame, width, height = _normalize_frame(frame, width, height)
                frame_started = time.monotonic()
                try:
                    raw_detections = self.registry.predict(
                        frame,
                        conf=conf,
                        imgsz=imgsz,
                        iou=iou,
                        max_det=max_det,
                        device=self.settings.device,
                    )
                except ModelUnavailable as exc:
                    raise InferenceError("model_unavailable", str(exc)) from exc
                except InferenceError:
                    raise
                except Exception as exc:  # pragma: no cover - model runtime specifics
                    raise InferenceError("inference_failed", f"推理失败: {exc}") from exc
                inference_ms = (time.monotonic() - frame_started) * 1000.0

                detections = [
                    make_detection(
                        frame_id=frame_index,
                        index=position,
                        class_id=int(item["class_id"]),
                        class_name=str(item["class_name"]),
                        confidence=float(item["confidence"]),
                        bbox=clamp_bbox(
                            item["bbox"]["x1"],
                            item["bbox"]["y1"],
                            item["bbox"]["x2"],
                            item["bbox"]["y2"],
                            width=width,
                            height=height,
                        ),
                    )
                    for position, item in enumerate(raw_detections)
                ]
                frames.append(
                    make_frame(
                        frame_id=frame_index,
                        timestamp_ms=timestamp_ms,
                        inference_ms=inference_ms,
                        detections=detections,
                    )
                )
                processed += 1
                if progress is not None:
                    progress(
                        min(99, int(processed * 100 / max(1, expected_samples))),
                        {
                            "processed_frames": processed,
                            "detection_count": sum(len(f["detections"]) for f in frames),
                            "expected_samples": expected_samples,
                            "sampled_every_n_frames": stride,
                        },
                    )
        finally:
            release = getattr(capture, "release", None)
            if callable(release):
                release()

        if processed == 0:
            raise InferenceError("empty_video", f"视频没有可解码的帧: {path.name}")

        finished = datetime.now(timezone.utc)
        model_info = self.registry.info()
        timestamp_source = (
            "pts" if pts_used > 0 and fallbacks == 0 else ("fps" if fps > 0 else "fallback_25fps")
        )
        video = {
            "filename": spec.filename or path.name,
            "width": width,
            "height": height,
            "fps": fps or None,
            "frame_count": read_index,
            "reported_frame_count": reported,
            "duration_ms": duration_ms,
            "duration_known": duration_ms is not None,
            "sampled_every_n_frames": stride,
            "processed_frames": processed,
            "size_bytes": size,
            "sha256": spec.video_sha256,
            "timestamp_source": timestamp_source,
            "max_samples": self.settings.max_samples,
            "target_samples": target_samples,
        }
        model = {
            "provider": "ultralytics",
            "model_name": model_info.get("model_name"),
            "model_version": model_info.get("model_version"),
            "weights_sha256": model_info.get("weights_sha256"),
            "device": self.settings.device,
            "imgsz": imgsz,
            "license": LICENSE_NAME,
        }
        return build_artifact(
            task_uid=spec.task_uid,
            device_id=spec.device_id,
            filename=spec.filename or path.name,
            model=model,
            video=video,
            params={
                "conf_threshold": conf,
                "iou": iou,
                "imgsz": imgsz,
                "max_det": max_det,
                "frame_stride": stride,
                "target_samples": target_samples,
            },
            frames=frames,
            created_at=spec.created_at.astimezone(timezone.utc).isoformat(),
            finished_at=finished.isoformat(),
        )
