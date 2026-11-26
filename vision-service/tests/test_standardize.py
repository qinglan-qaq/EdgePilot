from __future__ import annotations

from app.standardize import (
    SCHEMA_VERSION,
    artifact_digest,
    build_artifact,
    clamp_bbox,
    make_detection,
    make_frame,
    summarize,
)


def test_clamp_bbox_crops_to_frame():
    assert clamp_bbox(-5, 10, 700, 500, width=640, height=480) == {
        "x1": 0.0,
        "y1": 10.0,
        "x2": 640.0,
        "y2": 480.0,
    }


def test_clamp_bbox_normalizes_inverted_corners():
    assert clamp_bbox(100, 200, 20, 30, width=640, height=480) == {
        "x1": 20.0,
        "y1": 30.0,
        "x2": 100.0,
        "y2": 200.0,
    }


def test_summary_counts_empty_frames():
    frames = [
        make_frame(
            frame_id=0,
            timestamp_ms=0,
            inference_ms=10,
            detections=[
                make_detection(
                    frame_id=0,
                    index=0,
                    class_id=0,
                    class_name="person",
                    confidence=0.9,
                    bbox={"x1": 1, "y1": 2, "x2": 3, "y2": 4},
                )
            ],
        ),
        make_frame(frame_id=5, timestamp_ms=200, inference_ms=5, detections=[]),
    ]
    summary = summarize(frames)
    assert summary["processed_frames"] == 2
    assert summary["detection_count"] == 1
    assert summary["class_counts"] == {"person": 1}
    assert summary["peak_confidence"] == 0.9
    assert summary["inference_ms"] == 15


def test_detection_id_and_precision():
    detection = make_detection(
        frame_id=7,
        index=2,
        class_id=0,
        class_name="person",
        confidence=0.91234,
        bbox={"x1": 1.2345, "y1": 2.3456, "x2": 3.4567, "y2": 4.5678},
    )
    assert detection["detection_id"] == "7-2"
    assert detection["confidence"] == 0.9123
    assert detection["bbox"] == {"x1": 1.23, "y1": 2.35, "x2": 3.46, "y2": 4.57}


def test_artifact_has_contract_fields():
    artifact = build_artifact(
        task_uid="task-1",
        device_id="video-upload",
        filename="a.mp4",
        model={"model_name": "yolo11n"},
        video={"width": 10, "height": 10},
        params={"conf_threshold": 0.25},
        frames=[],
        created_at="2026-11-26T00:00:00+00:00",
        finished_at="2026-11-26T00:00:01+00:00",
    )
    assert artifact["schema_version"] == SCHEMA_VERSION == "vision-result/v1"
    assert artifact["event_id"] == "task-1"
    assert artifact["error"] is None
    assert artifact["source"]["type"] == "video_file"
    assert artifact["task"]["task_uid"] == "task-1"
    assert artifact["summary"]["processed_frames"] == 0


def test_artifact_digest_is_canonical_and_stable():
    artifact = {"b": 1, "a": [1, 2]}
    assert artifact_digest(artifact) == artifact_digest({"a": [1, 2], "b": 1})
    assert len(artifact_digest(artifact)) == 64
