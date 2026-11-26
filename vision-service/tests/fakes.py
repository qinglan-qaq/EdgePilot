from __future__ import annotations

import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from app.config import Settings
from app.inference import InferenceError, JobCancelled, JobSpec
from app.jobs import JobManager
from app.main import create_app
from app.model import ModelRegistry
from app.standardize import build_artifact, make_detection, make_frame


class FakeFrame:
    def __init__(self, height: int = 480, width: int = 640) -> None:
        self.shape = (height, width, 3)


class FakeCapture:
    def __init__(
        self,
        path: str = "",
        *,
        width: int = 640,
        height: int = 480,
        fps: float = 25.0,
        frame_count: int = 10,
        read_frames: int | None = None,
        pts: list[float] | None = None,
        opened: bool = True,
    ) -> None:
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self.frame_count = frame_count
        self.read_frames = frame_count if read_frames is None else read_frames
        self.pts = pts
        self.opened = opened
        self.index = 0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 - mirrors cv2 API
        return self.opened

    def get(self, prop: int) -> float:
        if prop == 0:  # CAP_PROP_POS_MSEC
            if self.pts is None:
                return 0.0
            position = max(0, self.index - 1)
            if position < len(self.pts):
                return float(self.pts[position])
            if self.pts:
                return float(self.pts[-1])
            return 0.0
        if prop == 3:  # CAP_PROP_FRAME_WIDTH
            return float(self.width)
        if prop == 4:  # CAP_PROP_FRAME_HEIGHT
            return float(self.height)
        if prop == 5:  # CAP_PROP_FPS
            return float(self.fps)
        if prop == 7:  # CAP_PROP_FRAME_COUNT
            return float(self.frame_count)
        return 0.0

    def read(self) -> tuple[bool, FakeFrame | None]:
        if self.index >= self.read_frames:
            return False, None
        self.index += 1
        return True, FakeFrame(self.height, self.width)

    def release(self) -> None:
        self.released = True


def install_fake_cv2(monkeypatch: Any, **capture_kwargs: Any) -> types.ModuleType:
    module = types.ModuleType("cv2")
    module.CAP_PROP_POS_MSEC = 0
    module.CAP_PROP_FRAME_WIDTH = 3
    module.CAP_PROP_FRAME_HEIGHT = 4
    module.CAP_PROP_FPS = 5
    module.CAP_PROP_FRAME_COUNT = 7
    module.CAP_PROP_POS_FRAMES = 1

    instances: list[FakeCapture] = []

    def factory(path: str) -> FakeCapture:
        capture = FakeCapture(path, **capture_kwargs)
        instances.append(capture)
        return capture

    module.VideoCapture = factory  # type: ignore[attr-defined]
    module.instances = instances  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cv2", module)
    return module


class FakeRegistry:
    def __init__(self, detections: Callable[[int], list[dict[str, Any]]] | None = None) -> None:
        self._detections = detections or (lambda _index: [])
        self.calls = 0

    def info(self) -> dict[str, Any]:
        return {
            "status": "ready",
            "load_status": "ready",
            "model_name": "yolo11n",
            "model_version": "yolo11n-abcdef123456",
            "weights_sha256": "a" * 64,
            "license": "AGPL-3.0",
            "device": "cpu",
            "imgsz": 640,
        }

    def predict(self, frame: Any, **_: Any) -> list[dict[str, Any]]:
        index = self.calls
        self.calls += 1
        return self._detections(index)


def person_detection(confidence: float = 0.9) -> dict[str, Any]:
    return {
        "class_id": 0,
        "class_name": "person",
        "confidence": confidence,
        "bbox": {"x1": 10.0, "y1": 20.0, "x2": 110.0, "y2": 220.0},
    }


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "jobs_dir": tmp_path / "jobs",
        "model_path": tmp_path / "weights" / "none.pt",
        "preload": False,
        "max_file_bytes": 200 * 1024 * 1024,
    }
    base.update(overrides)
    return Settings(**base)


def sample_artifact(task_uid: str = "task-0001") -> dict[str, Any]:
    frame = make_frame(
        frame_id=0,
        timestamp_ms=0,
        inference_ms=12.5,
        detections=[
            make_detection(
                frame_id=0,
                index=0,
                class_id=0,
                class_name="person",
                confidence=0.9,
                bbox={"x1": 10, "y1": 20, "x2": 110, "y2": 220},
            )
        ],
    )
    return build_artifact(
        task_uid=task_uid,
        device_id=None,
        filename="clip.mp4",
        model={
            "provider": "ultralytics",
            "model_name": "yolo11n",
            "model_version": "yolo11n-abcdef123456",
            "license": "AGPL-3.0",
        },
        video={
            "width": 640,
            "height": 480,
            "fps": 25.0,
            "frame_count": 10,
            "reported_frame_count": 10,
            "duration_ms": 400,
            "sampled_every_n_frames": 1,
            "processed_frames": 1,
            "timestamp_source": "pts",
        },
        params={"conf_threshold": 0.25},
        frames=[frame],
        created_at="2026-11-26T00:00:00+00:00",
        finished_at="2026-11-26T00:00:01+00:00",
    )


class ArtifactEngine:
    def __init__(self, artifact: dict[str, Any] | None = None) -> None:
        self.artifact = artifact or sample_artifact()

    def run(
        self,
        spec: JobSpec,
        progress: Callable[[int, dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if progress:
            progress(50, {"processed_frames": 1, "detection_count": 1})
        return self.artifact


class FailingEngine:
    def run(self, spec: JobSpec, progress: Any = None, cancelled: Any = None) -> Any:
        if progress:
            progress(10, {"processed_frames": 0, "detection_count": 0})
        raise InferenceError("inference_failed", "模拟推理失败")


class SlowEngine:
    """Checks the cancel flag like the real engine does between frames."""

    def run(
        self,
        spec: JobSpec,
        progress: Callable[[int, dict[str, Any]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        for step in range(200):
            if cancelled and cancelled():
                raise JobCancelled()
            if progress:
                progress(min(99, step), {"processed_frames": step, "detection_count": 0})
            time.sleep(0.01)
        return sample_artifact(spec.task_uid)


@contextmanager
def make_client(settings: Settings, *, engine: Any | None = None):
    registry = ModelRegistry(settings)
    engine = engine or ArtifactEngine()
    manager = JobManager(settings, engine)
    app = create_app(settings, registry=registry, engine=engine, manager=manager)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        manager.shutdown(wait=True)


def write_video(path: Path, size: int = 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def wait_for(
    manager: JobManager, task_uid: str, statuses: set[str], timeout: float = 5.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.get(task_uid)
        if state.get("status") in statuses:
            return state
        time.sleep(0.01)
    raise AssertionError(f"timeout waiting for {statuses}, last={manager.get(task_uid)}")
