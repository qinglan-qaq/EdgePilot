from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

SCHEMA_VERSION = "vision-result/v1"


def clamp_bbox(x1: float, y1: float, x2: float, y2: float, width: int, height: int) -> dict[str, float]:
    """Clamp a pixel bbox into the frame and normalize corner order."""

    left, right = (x1, x2) if x1 <= x2 else (x2, x1)
    top, bottom = (y1, y2) if y1 <= y2 else (y2, y1)
    left = min(max(float(left), 0.0), float(width))
    right = min(max(float(right), 0.0), float(width))
    top = min(max(float(top), 0.0), float(height))
    bottom = min(max(float(bottom), 0.0), float(height))
    return {
        "x1": round(left, 2),
        "y1": round(top, 2),
        "x2": round(right, 2),
        "y2": round(bottom, 2),
    }


def make_detection(
    *,
    frame_id: int,
    index: int,
    class_id: int,
    class_name: str,
    confidence: float,
    bbox: dict[str, float],
) -> dict[str, Any]:
    return {
        "detection_id": f"{frame_id}-{index}",
        "class_id": int(class_id),
        "class_name": str(class_name),
        "confidence": round(float(confidence), 4),
        "bbox": {key: round(float(bbox[key]), 2) for key in ("x1", "y1", "x2", "y2")},
    }


def make_frame(
    *,
    frame_id: int,
    timestamp_ms: int,
    inference_ms: float,
    detections: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "frame_id": int(frame_id),
        "timestamp_ms": int(timestamp_ms),
        "inference_ms": round(float(inference_ms), 2),
        "detections": list(detections),
    }


def summarize(frames: Iterable[dict[str, Any]]) -> dict[str, Any]:
    processed = 0
    detection_count = 0
    class_counts: dict[str, int] = {}
    peak = 0.0
    inference_ms = 0.0
    for frame in frames:
        processed += 1
        inference_ms += float(frame.get("inference_ms") or 0.0)
        for detection in frame.get("detections") or []:
            detection_count += 1
            name = str(detection.get("class_name"))
            class_counts[name] = class_counts.get(name, 0) + 1
            peak = max(peak, float(detection.get("confidence") or 0.0))
    return {
        "processed_frames": processed,
        "detection_count": detection_count,
        "class_counts": class_counts,
        "peak_confidence": round(peak, 4),
        "inference_ms": int(round(inference_ms)),
    }


def build_artifact(
    *,
    task_uid: str,
    device_id: str | None,
    filename: str,
    model: dict[str, Any],
    video: dict[str, Any],
    params: dict[str, Any],
    frames: list[dict[str, Any]],
    created_at: str,
    finished_at: str,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": task_uid,
        "source": {"type": "video_file", "filename": filename},
        "task": {"task_uid": task_uid, "device_id": device_id},
        "model": model,
        "video": video,
        "params": params,
        "frames": frames,
        "summary": summarize(frames),
        "error": error,
        "created_at": created_at,
        "finished_at": finished_at,
    }


def artifact_digest(artifact: dict[str, Any]) -> str:
    payload = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
