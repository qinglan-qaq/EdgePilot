from __future__ import annotations

import pytest

from app.inference import InferenceError, JobCancelled, JobSpec, YoloEngine, probe_video
from fakes import FakeRegistry, install_fake_cv2, make_settings, write_video


def _engine(tmp_path, monkeypatch, capture: dict, settings: dict | None = None) -> YoloEngine:
    install_fake_cv2(monkeypatch, **capture)
    return YoloEngine(FakeRegistry(), make_settings(tmp_path, **(settings or {})))


def _run(engine: YoloEngine, video, params: dict | None = None, cancelled=None):
    return engine.run(
        JobSpec(
            task_uid="task-unit",
            video_path=str(video),
            filename="clip.mp4",
            params=params or {"conf_threshold": 0.25},
        ),
        lambda _p, _m: None,
        cancelled or (lambda: False),
    )


def test_pts_drives_timestamps(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(
        tmp_path, monkeypatch, {"frame_count": 5, "fps": 25.0, "pts": [0, 40, 80, 120, 160]}
    )
    artifact = _run(engine, video)
    assert [frame["timestamp_ms"] for frame in artifact["frames"]] == [0, 40, 80, 120, 160]
    assert artifact["video"]["timestamp_source"] == "pts"


def test_zero_pts_falls_back_to_fps(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(
        tmp_path, monkeypatch, {"frame_count": 5, "fps": 25.0, "pts": [0, 0, 0, 0, 0]}
    )
    artifact = _run(engine, video)
    assert [frame["timestamp_ms"] for frame in artifact["frames"]] == [0, 40, 80, 120, 160]
    assert artifact["video"]["timestamp_source"] == "fps"


def test_target_samples_drives_stride(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(tmp_path, monkeypatch, {"fps": 30.0, "frame_count": 2700})
    artifact = _run(engine, video, params={"target_samples": 900})
    assert artifact["video"]["sampled_every_n_frames"] == 3
    assert artifact["video"]["processed_frames"] == 900
    assert artifact["frames"][0]["frame_id"] == 0
    assert artifact["frames"][1]["frame_id"] == 3


def test_hard_frame_cap_rejects_unknown_frame_count(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(
        tmp_path,
        monkeypatch,
        {"fps": 25.0, "frame_count": 0, "read_frames": 10},
        settings={"max_samples": 3},
    )
    with pytest.raises(InferenceError) as excinfo:
        _run(engine, video)
    assert excinfo.value.code == "frame_limit_exceeded"


def test_video_too_long_is_rejected(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(tmp_path, monkeypatch, {"fps": 30.0, "frame_count": 900 * 30 + 1})
    with pytest.raises(InferenceError) as excinfo:
        _run(engine, video)
    assert excinfo.value.code == "video_too_long"


def test_probe_returns_real_metadata(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    install_fake_cv2(
        monkeypatch, fps=25.0, frame_count=250, width=1280, height=720
    )
    info = probe_video(video, make_settings(tmp_path), FakeRegistry())
    assert info["width"] == 1280
    assert info["height"] == 720
    assert info["fps"] == 25.0
    assert info["frame_count"] == 250
    assert info["duration_ms"] == 10000
    assert info["duration_known"] is True
    assert info["model_name"] == "yolo11n"
    assert info["model_version"].startswith("yolo11n-")


def test_file_too_large_is_rejected(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4", size=100)
    install_fake_cv2(monkeypatch, frame_count=10)
    with pytest.raises(InferenceError) as excinfo:
        probe_video(video, make_settings(tmp_path, max_file_bytes=16), FakeRegistry())
    assert excinfo.value.code == "file_too_large"


def test_empty_file_is_rejected(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4", size=0)
    install_fake_cv2(monkeypatch, frame_count=10)
    with pytest.raises(InferenceError) as excinfo:
        probe_video(video, make_settings(tmp_path), FakeRegistry())
    assert excinfo.value.code == "empty_video"


def test_cancelled_before_first_frame(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(tmp_path, monkeypatch, {"frame_count": 10})
    with pytest.raises(JobCancelled):
        _run(engine, video, cancelled=lambda: True)


def test_empty_video_without_readable_frames(tmp_path, monkeypatch):
    video = write_video(tmp_path / "clip.mp4")
    engine = _engine(tmp_path, monkeypatch, {"frame_count": 0})
    with pytest.raises(InferenceError) as excinfo:
        _run(engine, video)
    assert excinfo.value.code == "empty_video"
