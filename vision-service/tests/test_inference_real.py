"""Gated end-to-end test against real YOLO11n weights.

This test intentionally uses the real ``ModelRegistry`` + ``YoloEngine`` with
OpenCV's official ``vtest.avi`` sample and the Ultralytics ``yolo11n.pt``
weights. It must never be satisfied with fakes/mocks: it is skipped unless
``RUN_REAL_YOLO=1`` is set, and a missing model or failed download is reported
as an explicit failure instead of a fake pass.

Run:
    python scripts/fetch_model.py
    RUN_REAL_YOLO=1 VISION_MODEL_PATH=weights/yolo11n.pt \
        python -m pytest tests/test_inference_real.py -v
"""

from __future__ import annotations

import hashlib
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.config import Settings
from app.inference import JobSpec, YoloEngine
from app.model import ModelRegistry

# OpenCV official sample video (people walking through a plaza).
VTEST_URL = "https://raw.githubusercontent.com/opencv/opencv/master/samples/data/vtest.avi"
USER_AGENT = "edgepilot-vision-real-test/1.0"
DOWNLOAD_TIMEOUT_S = 180
CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_vtest(tmp_path: Path) -> Path:
    """Download ``vtest.avi`` into ``tmp_path`` and print its URL and SHA-256."""

    destination = tmp_path / "vtest.avi"
    print(f"[real-yolo] vtest.avi url={VTEST_URL}", flush=True)
    request = urllib.request.Request(VTEST_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise OSError(f"unexpected HTTP status {status}")
            with destination.open("wb") as handle:
                for chunk in iter(lambda: response.read(CHUNK_SIZE), b""):
                    handle.write(chunk)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        pytest.fail(f"failed to download vtest.avi from {VTEST_URL}: {exc!r}")

    size = destination.stat().st_size
    if size <= 0:
        pytest.fail(f"downloaded vtest.avi is empty: {destination}")
    digest = sha256_file(destination)
    print(f"[real-yolo] vtest.avi size={size} bytes sha256={digest}", flush=True)
    return destination


def _model_path() -> Path:
    return Path(os.environ.get("VISION_MODEL_PATH", "weights/yolo11n.pt"))


@pytest.mark.skipif(os.environ.get("RUN_REAL_YOLO") != "1", reason="set RUN_REAL_YOLO=1")
def test_real_video_detects_person(tmp_path: Path) -> None:
    video = download_vtest(tmp_path)

    model_path = _model_path()
    if not model_path.is_file() or model_path.stat().st_size == 0:
        pytest.fail(
            f"weights missing or empty: {model_path}; run "
            "`python scripts/fetch_model.py` from vision-service first"
        )

    settings = Settings(
        jobs_dir=tmp_path / "jobs",
        model_path=model_path,
        preload=True,
        max_samples=2000,
        target_samples=900,
    )
    registry = ModelRegistry(settings)
    engine = YoloEngine(registry, settings)
    artifact = engine.run(
        JobSpec(
            task_uid="real-video-0001",
            video_path=str(video),
            filename=video.name,
            params={"conf_threshold": 0.25, "target_samples": 900},
        ),
        lambda *_args: None,
        lambda: False,
    )

    assert artifact["summary"]["processed_frames"] > 0
    assert artifact["summary"]["detection_count"] >= 1

    person_detections = [
        detection
        for frame in artifact["frames"]
        for detection in frame["detections"]
        if detection["class_name"] == "person"
    ]
    assert person_detections, "real YOLO run produced no person detections"
    assert artifact["model"]["license"] == "AGPL-3.0"
    assert artifact["model"]["provider"] == "ultralytics"

    # The model must have really been loaded from local weights, not faked.
    assert registry.load_status == "ready"
    weights_sha256 = artifact["model"]["weights_sha256"]
    assert weights_sha256 and len(weights_sha256) == 64
    assert weights_sha256 == registry.weights_sha256

    # Every detected person box must lie inside the decoded video frame.
    width = float(artifact["video"]["width"])
    height = float(artifact["video"]["height"])
    assert width > 0 and height > 0
    for detection in person_detections:
        bbox = detection["bbox"]
        assert all(math.isfinite(bbox[key]) for key in ("x1", "y1", "x2", "y2")), bbox
        assert 0.0 <= bbox["x1"] < bbox["x2"] <= width, bbox
        assert 0.0 <= bbox["y1"] < bbox["y2"] <= height, bbox
        assert 0.0 < detection["confidence"] <= 1.0, detection
