"""视觉规则核心：几何、检测器抽象、4 类规则、截图与入库服务。

设计约束（Task 19）：
- bbox 统一为 ``(x, y, w, h)``；
- 规则只产出候选事件，不触库；入库与截图由 :mod:`app.vision.service` 负责；
- MockDetector 产物显式标记 ``mock-v1``，绝不伪造检测数据；
- 环境不依赖 numpy / ultralytics。
"""

from app.vision.detector import Detection, MockDetector, YoloDetector, create_detector
from app.vision.geometry import bbox_center, point_in_polygon, segments_intersect
from app.vision.rules import (
    CountRule,
    DwellRule,
    Frame,
    LineRule,
    RuleEngine,
    RuleEventCandidate,
    ZoneRule,
    bucket_for,
    build_rules,
)
from app.vision.snapshot import SnapshotResult, capture_snapshot, resolve_snapshot_path

__all__ = [
    "Detection",
    "MockDetector",
    "YoloDetector",
    "create_detector",
    "bbox_center",
    "point_in_polygon",
    "segments_intersect",
    "Frame",
    "RuleEventCandidate",
    "RuleEngine",
    "ZoneRule",
    "LineRule",
    "DwellRule",
    "CountRule",
    "build_rules",
    "bucket_for",
    "SnapshotResult",
    "capture_snapshot",
    "resolve_snapshot_path",
]
