"""视觉规则核心单测（Task 19）。

覆盖：射线法区域判定（含边界/顶点）、越线方向与真实相交、5min 桶去重、
dwell 阈值与离开重置、count 连续帧、MockDetector 确定性、Yolo 骨架、
截图降级占位图与路径穿越防护。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.vision.detector import (
    Detection,
    MockDetector,
    YoloDetector,
    create_detector,
)
from app.vision.geometry import bbox_center, point_in_polygon, segments_intersect
from app.vision.rules import (
    CountRule,
    DwellRule,
    Frame,
    LineRule,
    RuleEngine,
    ZoneRule,
    bucket_for,
    build_rules,
)
from app.vision.snapshot import capture_snapshot, resolve_snapshot_path

T0 = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
L_SHAPE = [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 10.0), (0.0, 10.0)]


def det(track_id: int | None, x: float, y: float, confidence: float = 0.9) -> Detection:
    """零尺寸 bbox：中心点即 (x, y)，便于几何断言。"""
    return Detection(label="person", bbox=(x, y, 0.0, 0.0), confidence=confidence, track_id=track_id)


def frame(at: datetime, detections: list[Detection], device_id: int = 1) -> Frame:
    return Frame(device_id=device_id, occurred_at=at, detections=detections)


# ---------------------------------------------------------------- 1. 几何 / 多边形


def test_point_in_polygon_inside_outside_boundary_vertex():
    assert point_in_polygon((5.0, 5.0), SQUARE) is True
    assert point_in_polygon((15.0, 5.0), SQUARE) is False
    # 边界点与顶点必须稳定判为“在内”。
    assert point_in_polygon((5.0, 0.0), SQUARE) is True
    assert point_in_polygon((10.0, 5.0), SQUARE) is True
    assert point_in_polygon((0.0, 0.0), SQUARE) is True
    assert point_in_polygon((10.0, 10.0), SQUARE) is True


def test_point_in_polygon_concave():
    assert point_in_polygon((2.0, 8.0), L_SHAPE) is True
    assert point_in_polygon((6.0, 6.0), L_SHAPE) is False  # 凹口外
    assert point_in_polygon((4.0, 6.0), L_SHAPE) is True  # 内折边


def test_bbox_center_and_segments_intersect():
    assert bbox_center((2.0, 4.0, 6.0, 8.0)) == (5.0, 8.0)
    # 真交叉
    assert segments_intersect((2.0, 5.0), (8.0, 5.0), (5.0, 0.0), (5.0, 10.0)) is True
    # 线段外，仅延长线相交
    assert segments_intersect((2.0, 20.0), (8.0, 20.0), (5.0, 0.0), (5.0, 10.0)) is False


# ---------------------------------------------------------------- 2. 越线方向


def _line_engine() -> RuleEngine:
    return RuleEngine([LineRule(line_id="L1", start=(5.0, 0.0), end=(5.0, 10.0))])


def test_line_crossing_direction_both_ways():
    engine = _line_engine()
    engine.process(frame(T0, [det(7, 2.0, 5.0)]))
    events = engine.process(frame(T0 + timedelta(seconds=1), [det(7, 8.0, 5.0)]))
    assert len(events) == 1
    assert events[0].payload["direction"] == "positive_to_negative"
    assert events[0].dedup_key == f"1:line:7:{bucket_for(T0)}"

    reverse = _line_engine()
    reverse.process(frame(T0, [det(8, 8.0, 5.0)]))
    events = reverse.process(frame(T0 + timedelta(seconds=1), [det(8, 2.0, 5.0)]))
    assert len(events) == 1
    assert events[0].payload["direction"] == "negative_to_positive"


def test_line_no_event_when_segments_do_not_intersect():
    engine = _line_engine()
    engine.process(frame(T0, [det(7, 2.0, 20.0)]))
    assert engine.process(frame(T0 + timedelta(seconds=1), [det(7, 8.0, 20.0)])) == []


def test_line_same_side_is_not_repeated():
    engine = _line_engine()
    engine.process(frame(T0, [det(7, 2.0, 5.0)]))
    assert engine.process(frame(T0 + timedelta(seconds=1), [det(7, 3.0, 5.0)])) == []
    assert engine.process(frame(T0 + timedelta(seconds=2), [det(7, 4.0, 5.0)])) == []


# ---------------------------------------------------------------- 3. 5min 桶去重


def test_zone_dedup_per_track_per_bucket():
    engine = RuleEngine([ZoneRule(zone_id="Z1", polygon=SQUARE)])
    inside = [det(7, 5.0, 5.0)]
    assert len(engine.process(frame(T0, inside))) == 1
    # 同桶重复帧不再发
    assert engine.process(frame(T0 + timedelta(seconds=30), inside)) == []
    # 跨桶可再发
    later = T0 + timedelta(seconds=301)
    assert later != T0 and bucket_for(later) != bucket_for(T0)
    assert len(engine.process(frame(later, inside))) == 1


def test_zone_skips_detections_without_track_id():
    engine = RuleEngine([ZoneRule(zone_id="Z1", polygon=SQUARE)])
    assert engine.process(frame(T0, [det(None, 5.0, 5.0)])) == []


def test_build_rules_from_device_config():
    rules = build_rules(
        {
            "rules": {
                "zone": [{"zone_id": "Z1", "polygon": SQUARE, "severity": "critical"}],
                "line": {"line_id": "L1", "start": [5, 0], "end": [5, 10]},
                "dwell": [{"zone_id": "Z1", "polygon": SQUARE, "seconds": 10}],
                "count": [{"zone_id": "Z1", "polygon": SQUARE, "threshold": 3,
                           "consecutive_frames": 2}],
            }
        }
    )
    assert [r.rule for r in rules] == ["zone", "line", "dwell", "count"]
    assert build_rules({}) == []
    with pytest.raises(ValueError):
        build_rules({"rules": {"zone": [{"zone_id": "Z1", "polygon": SQUARE}],
                               "unknown": []}})
    with pytest.raises(ValueError):
        build_rules({"rules": {"zone": [{"zone_id": "Z1", "polygon": [(0, 0), (1, 1)]}]}})


# ---------------------------------------------------------------- 4. dwell


def test_dwell_threshold_once_per_bucket_and_reset_on_leave():
    engine = RuleEngine([DwellRule(zone_id="Z1", polygon=SQUARE, seconds=10)])
    inside = [det(3, 5.0, 5.0)]
    assert engine.process(frame(T0, inside)) == []
    assert engine.process(frame(T0 + timedelta(seconds=6), inside)) == []
    events = engine.process(frame(T0 + timedelta(seconds=11), inside))
    assert len(events) == 1
    assert events[0].payload["dwell_seconds"] >= 10
    # 同桶不再重复
    assert engine.process(frame(T0 + timedelta(seconds=12), inside)) == []

    # 离开区域后累计清零：再进来 6s 不足以触发
    reset = RuleEngine([DwellRule(zone_id="Z1", polygon=SQUARE, seconds=10)])
    reset.process(frame(T0, inside))
    reset.process(frame(T0 + timedelta(seconds=6), inside))
    reset.process(frame(T0 + timedelta(seconds=7), [det(3, 50.0, 50.0)]))
    reset.process(frame(T0 + timedelta(seconds=8), inside))
    assert reset.process(frame(T0 + timedelta(seconds=14), inside)) == []


# ---------------------------------------------------------------- 5. count


def _count_engine() -> RuleEngine:
    return RuleEngine(
        [CountRule(zone_id="Z1", polygon=SQUARE, threshold=3, consecutive_frames=3)]
    )


def _three() -> list[Detection]:
    return [det(1, 1.0, 1.0), det(2, 2.0, 2.0), det(3, 3.0, 3.0)]


def test_count_requires_consecutive_frames_and_dedups_per_bucket():
    engine = _count_engine()
    assert engine.process(frame(T0, _three())) == []
    assert engine.process(frame(T0 + timedelta(seconds=1), _three()[:2])) == []
    assert engine.process(frame(T0 + timedelta(seconds=2), _three())) == []
    assert engine.process(frame(T0 + timedelta(seconds=3), _three())) == []
    events = engine.process(frame(T0 + timedelta(seconds=4), _three()))
    assert len(events) == 1
    assert events[0].track_id is None
    assert events[0].dedup_key == f"1:count:{bucket_for(T0)}"
    assert events[0].payload["count"] == 3
    # 同桶只一次
    assert engine.process(frame(T0 + timedelta(seconds=5), _three())) == []

    # 跨桶可再发（连续帧状态仍满足，进入新桶即重新触发一次）
    later = T0 + timedelta(seconds=301)
    events = engine.process(frame(later, _three()))
    assert len(events) == 1
    assert events[0].dedup_key == f"1:count:{bucket_for(later)}"
    assert engine.process(frame(later + timedelta(seconds=1), _three())) == []


def test_count_works_without_track_ids():
    engine = RuleEngine(
        [CountRule(zone_id="Z1", polygon=SQUARE, threshold=2, consecutive_frames=1)]
    )
    events = engine.process(frame(T0, [det(None, 1.0, 1.0), det(None, 2.0, 2.0)]))
    assert len(events) == 1


# ---------------------------------------------------------------- 6. detector


def test_mock_detector_is_deterministic_and_explicitly_empty():
    script = [[det(1, 1.0, 1.0)], []]
    a = MockDetector(script)
    b = MockDetector(script)
    assert a.detect(object()) == script[0]
    assert a.detect(object()) == []
    assert a.detect(object()) == []
    assert b.detect(None) == script[0]
    assert a.model_version == "mock-v1"
    assert a.is_mock is True
    # 无轨迹时显式空列表，不随机、不编造。
    assert MockDetector().detect(None) == []


def test_create_detector_and_yolo_skeleton():
    assert isinstance(create_detector({"detector": "mock"}), MockDetector)
    with pytest.raises(ValueError):
        create_detector({"detector": "unknown"})
    with pytest.raises(NotImplementedError):
        YoloDetector()
    with pytest.raises(NotImplementedError):
        create_detector({"detector": "yolo"})


# ---------------------------------------------------------------- 7. snapshot


def _fake_success(cmd: list[str]) -> None:
    Path(cmd[-1]).write_bytes(b"\xff\xd8\xff\xd9")


def _fake_missing(cmd: list[str]) -> None:
    raise FileNotFoundError("ffmpeg 未安装")


def test_snapshot_success_uses_controlled_path(tmp_path: Path):
    result = capture_snapshot(
        "rtsp://demo/stream", T0, tmp_path, ffmpeg_runner=_fake_success, device_id="42"
    )
    assert result.degraded is False and result.error is None
    assert result.path is not None
    assert result.path.startswith("snapshots/events/2026-11-25/")
    assert (tmp_path / result.path).is_file()


def test_snapshot_failure_falls_back_to_placeholder(tmp_path: Path):
    result = capture_snapshot(
        "rtsp://demo/stream", T0, tmp_path, ffmpeg_runner=_fake_missing, device_id="42"
    )
    assert result.degraded is True
    assert "ffmpeg" in (result.error or "")
    path = tmp_path / result.path
    assert path.is_file()
    assert path.read_bytes().startswith(b"\xff\xd8")  # 真实 JPEG，不是伪造截图


def test_resolve_snapshot_path_rejects_traversal(tmp_path: Path):
    assert resolve_snapshot_path(tmp_path, "snapshots/events/x.jpg") == (
        tmp_path.resolve() / "snapshots/events/x.jpg"
    )
    for bad in ("../evil.jpg", "snapshots/../../evil.jpg", str(tmp_path / "abs.jpg")):
        with pytest.raises(ValueError):
            resolve_snapshot_path(tmp_path, bad)
