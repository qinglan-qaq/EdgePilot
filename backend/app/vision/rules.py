"""4 类视觉规则与规则引擎。

规则只读 ``Frame`` 并产出 :class:`RuleEventCandidate`，不触库、不抓图。状态型
规则（dwell / count）跨帧保存状态；5 分钟桶去重由 :class:`RuleEngine` 统一兜底，
规则自身也只在桶内首次满足条件时发出候选。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from app.vision.detector import Detection
from app.vision.geometry import (
    BBox,
    Point,
    bbox_center,
    cross_sign,
    point_in_polygon,
    segments_intersect,
)

RULE_TYPES = ("zone", "line", "dwell", "count")
BUCKET_SECONDS = 300


def bucket_for(occurred_at: datetime) -> int:
    """UTC 下 ``int(timestamp // 300)``；naive 时间按 UTC 解释。"""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return int(occurred_at.timestamp() // BUCKET_SECONDS)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def bbox_dict(bbox: BBox) -> dict[str, float]:
    x, y, w, h = (float(v) for v in bbox)
    return {"x": x, "y": y, "w": w, "h": h}


@dataclass(frozen=True)
class Frame:
    """一帧输入。``detections`` 由调用方用 detector 生成后再交给规则引擎。"""

    device_id: int
    occurred_at: datetime
    detections: Sequence[Detection]
    node_id: int | None = None
    frame_index: int | None = None


@dataclass
class RuleEventCandidate:
    device_id: int
    rule: str
    event_type: str
    severity: str
    occurred_at: datetime
    confidence: float
    track_id: str | None
    bbox: dict | None
    dedup_key: str
    payload: dict


def _track_id(detection: Detection) -> str | None:
    return None if detection.track_id is None else str(detection.track_id)


def _dedup_key(device_id: int, rule: str, track_id: str | None, bucket: int) -> str:
    if track_id is None:
        return f"{device_id}:{rule}:{bucket}"
    return f"{device_id}:{rule}:{track_id}:{bucket}"


class Rule(Protocol):
    rule: str

    def evaluate(self, frame: Frame) -> list[RuleEventCandidate]: ...


def _validate_polygon(value: Any, *, field_name: str) -> tuple[Point, ...]:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise ValueError(f"{field_name} 必须是至少 3 个点的数组")
    points: list[Point] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"{field_name} 的每个点必须是 [x, y]")
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} 坐标必须是数字") from exc
        points.append((x, y))
    return tuple(points)


def _validate_point(value: Any, *, field_name: str) -> Point:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field_name} 必须是 [x, y]")
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 坐标必须是数字") from exc


def _require_id(spec: dict, key: str, rule: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{rule} 规则缺少 {key}")
    return value.strip()


def _severity(spec: dict) -> str:
    value = spec.get("severity", "high")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("severity 必须是非空字符串")
    return value.strip()


class ZoneRule:
    """区域入侵：bbox 中心点落在多边形内。"""

    rule = "zone"

    def __init__(
        self,
        *,
        zone_id: str,
        polygon: Iterable[Point],
        severity: str = "high",
        event_type: str = "zone",
    ) -> None:
        self.zone_id = zone_id
        self.polygon = _validate_polygon(list(polygon), field_name="polygon")
        self.severity = severity
        self.event_type = event_type

    def evaluate(self, frame: Frame) -> list[RuleEventCandidate]:
        bucket = bucket_for(frame.occurred_at)
        out: list[RuleEventCandidate] = []
        for detection in frame.detections:
            track_id = _track_id(detection)
            if track_id is None:
                # 不编造 track_id：需要目标的规则跳过无名检测。
                continue
            if not point_in_polygon(bbox_center(detection.bbox), self.polygon):
                continue
            out.append(
                RuleEventCandidate(
                    device_id=frame.device_id,
                    rule=self.rule,
                    event_type=self.event_type,
                    severity=self.severity,
                    occurred_at=frame.occurred_at,
                    confidence=float(detection.confidence),
                    track_id=track_id,
                    bbox=bbox_dict(detection.bbox),
                    dedup_key=_dedup_key(frame.device_id, self.rule, track_id, bucket),
                    payload={"zone_id": self.zone_id, "rule": self.rule},
                )
            )
        return out


class LineRule:
    """越线：前后帧中心点与线段叉积符号变化，且放大线段真实相交。"""

    rule = "line"

    def __init__(
        self,
        *,
        line_id: str,
        start: Point,
        end: Point,
        severity: str = "high",
        event_type: str = "line",
    ) -> None:
        self.line_id = line_id
        self.start = (float(start[0]), float(start[1]))
        self.end = (float(end[0]), float(end[1]))
        if self.start == self.end:
            raise ValueError("line 规则 start/end 不能相同")
        self.severity = severity
        self.event_type = event_type
        self._last: dict[tuple[int, str], Point] = {}

    def evaluate(self, frame: Frame) -> list[RuleEventCandidate]:
        bucket = bucket_for(frame.occurred_at)
        out: list[RuleEventCandidate] = []
        for detection in frame.detections:
            track_id = _track_id(detection)
            if track_id is None:
                continue
            key = (frame.device_id, track_id)
            current = bbox_center(detection.bbox)
            previous = self._last.get(key)
            self._last[key] = current
            if previous is None:
                continue

            before = cross_sign(self.start, self.end, previous)
            after = cross_sign(self.start, self.end, current)
            if before == 0 or after == 0 or before == after:
                continue
            if not segments_intersect(previous, current, self.start, self.end):
                continue

            direction = (
                "positive_to_negative" if before > 0 else "negative_to_positive"
            )
            out.append(
                RuleEventCandidate(
                    device_id=frame.device_id,
                    rule=self.rule,
                    event_type=self.event_type,
                    severity=self.severity,
                    occurred_at=frame.occurred_at,
                    confidence=float(detection.confidence),
                    track_id=track_id,
                    bbox=bbox_dict(detection.bbox),
                    dedup_key=_dedup_key(frame.device_id, self.rule, track_id, bucket),
                    payload={
                        "line_id": self.line_id,
                        "direction": direction,
                        "rule": self.rule,
                    },
                )
            )
        return out


@dataclass
class _DwellState:
    inside: bool = False
    total_seconds: float = 0.0
    last_at: datetime | None = None
    emitted_buckets: set[int] = field(default_factory=set)


class DwellRule:
    """停留：同一 track 在区域内累计时长达到阈值，离开区域即清零。"""

    rule = "dwell"

    def __init__(
        self,
        *,
        zone_id: str,
        polygon: Iterable[Point],
        seconds: float,
        severity: str = "high",
        event_type: str = "dwell",
    ) -> None:
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
            raise ValueError("dwell.seconds 必须是正数")
        self.zone_id = zone_id
        self.polygon = _validate_polygon(list(polygon), field_name="polygon")
        self.seconds = float(seconds)
        self.severity = severity
        self.event_type = event_type
        self._states: dict[tuple[int, str], _DwellState] = {}

    def evaluate(self, frame: Frame) -> list[RuleEventCandidate]:
        bucket = bucket_for(frame.occurred_at)
        now = _as_utc(frame.occurred_at)
        out: list[RuleEventCandidate] = []
        for detection in frame.detections:
            track_id = _track_id(detection)
            if track_id is None:
                continue
            key = (frame.device_id, track_id)
            state = self._states.setdefault(key, _DwellState())
            inside = point_in_polygon(bbox_center(detection.bbox), self.polygon)

            if not inside:
                state.inside = False
                state.total_seconds = 0.0
                state.last_at = now
                continue

            if not state.inside:
                state.inside = True
                state.total_seconds = 0.0
            elif state.last_at is not None:
                state.total_seconds += max(0.0, (now - state.last_at).total_seconds())
            state.last_at = now

            if state.total_seconds < self.seconds:
                continue
            if bucket in state.emitted_buckets:
                continue
            state.emitted_buckets.add(bucket)
            out.append(
                RuleEventCandidate(
                    device_id=frame.device_id,
                    rule=self.rule,
                    event_type=self.event_type,
                    severity=self.severity,
                    occurred_at=frame.occurred_at,
                    confidence=float(detection.confidence),
                    track_id=track_id,
                    bbox=bbox_dict(detection.bbox),
                    dedup_key=_dedup_key(frame.device_id, self.rule, track_id, bucket),
                    payload={
                        "zone_id": self.zone_id,
                        "dwell_seconds": round(state.total_seconds, 3),
                        "threshold_seconds": self.seconds,
                        "rule": self.rule,
                    },
                )
            )
        return out


@dataclass
class _CountState:
    consecutive: int = 0
    fired_buckets: set[int] = field(default_factory=set)


class CountRule:
    """数量超限：区域内目标数达到阈值并连续 N 帧，同一 5min 桶只报一次。"""

    rule = "count"

    def __init__(
        self,
        *,
        zone_id: str,
        polygon: Iterable[Point],
        threshold: int,
        consecutive_frames: int = 1,
        severity: str = "high",
        event_type: str = "count",
    ) -> None:
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise ValueError("count.threshold 必须是正整数")
        if (
            not isinstance(consecutive_frames, int)
            or isinstance(consecutive_frames, bool)
            or consecutive_frames < 1
        ):
            raise ValueError("count.consecutive_frames 必须是正整数")
        self.zone_id = zone_id
        self.polygon = _validate_polygon(list(polygon), field_name="polygon")
        self.threshold = threshold
        self.consecutive_frames = consecutive_frames
        self.severity = severity
        self.event_type = event_type
        self._states: dict[int, _CountState] = {}

    def evaluate(self, frame: Frame) -> list[RuleEventCandidate]:
        bucket = bucket_for(frame.occurred_at)
        state = self._states.setdefault(frame.device_id, _CountState())

        inside = [
            detection
            for detection in frame.detections
            if point_in_polygon(bbox_center(detection.bbox), self.polygon)
        ]
        count = len(inside)
        if count >= self.threshold:
            state.consecutive += 1
        else:
            state.consecutive = 0

        if state.consecutive < self.consecutive_frames:
            return []
        if bucket in state.fired_buckets:
            return []
        state.fired_buckets.add(bucket)

        xs = [d.bbox[0] for d in inside]
        ys = [d.bbox[1] for d in inside]
        x2s = [d.bbox[0] + d.bbox[2] for d in inside]
        y2s = [d.bbox[1] + d.bbox[3] for d in inside]
        bbox = bbox_dict((min(xs), min(ys), max(x2s) - min(xs), max(y2s) - min(ys)))

        return [
            RuleEventCandidate(
                device_id=frame.device_id,
                rule=self.rule,
                event_type=self.event_type,
                severity=self.severity,
                occurred_at=frame.occurred_at,
                confidence=min(float(d.confidence) for d in inside),
                track_id=None,
                bbox=bbox,
                dedup_key=_dedup_key(frame.device_id, self.rule, None, bucket),
                payload={
                    "zone_id": self.zone_id,
                    "count": count,
                    "threshold": self.threshold,
                    "consecutive_frames": self.consecutive_frames,
                    "rule": self.rule,
                },
            )
        ]


class RuleEngine:
    """把一帧交给全部规则，并在同一设备同一 5min 桶内按 ``dedup_key`` 去重。"""

    def __init__(self, rules: Iterable[Rule]) -> None:
        self.rules = list(rules)
        self._buckets: dict[int, int] = {}
        self._emitted: set[str] = set()

    def process(self, frame: Frame) -> list[RuleEventCandidate]:
        bucket = bucket_for(frame.occurred_at)
        if self._buckets.get(frame.device_id) != bucket:
            previous = self._buckets.get(frame.device_id)
            self._buckets[frame.device_id] = bucket
            if previous is not None:
                prefix = f"{frame.device_id}:"
                self._emitted = {
                    key for key in self._emitted if not key.startswith(prefix)
                }

        out: list[RuleEventCandidate] = []
        for rule in self.rules:
            for candidate in rule.evaluate(frame):
                if candidate.dedup_key in self._emitted:
                    continue
                self._emitted.add(candidate.dedup_key)
                out.append(candidate)
        return out


def _rule_specs(config: dict, name: str) -> list[dict]:
    value = config.get(name)
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(f"rules.{name} 的每项必须是对象")
        return value
    raise ValueError(f"rules.{name} 必须是对象或对象数组")


def build_rules(device_config: dict | None) -> list[Rule]:
    """从 ``devices.config["rules"]`` 构建规则；缺失即无规则，非法配置抛 ValueError。"""
    if device_config is None:
        return []
    if not isinstance(device_config, dict):
        raise ValueError("device_config 必须是对象")
    config = device_config.get("rules")
    if config is None:
        return []
    if not isinstance(config, dict):
        raise ValueError("devices.config.rules 必须是对象")

    unknown = set(config) - set(RULE_TYPES)
    if unknown:
        raise ValueError(f"未知视觉规则类型: {sorted(unknown)}")

    rules: list[Rule] = []
    for spec in _rule_specs(config, "zone"):
        rules.append(
            ZoneRule(
                zone_id=_require_id(spec, "zone_id", "zone"),
                polygon=spec.get("polygon"),
                severity=_severity(spec),
                event_type=spec.get("event_type", "zone"),
            )
        )
    for spec in _rule_specs(config, "line"):
        rules.append(
            LineRule(
                line_id=_require_id(spec, "line_id", "line"),
                start=_validate_point(spec.get("start"), field_name="line.start"),
                end=_validate_point(spec.get("end"), field_name="line.end"),
                severity=_severity(spec),
                event_type=spec.get("event_type", "line"),
            )
        )
    for spec in _rule_specs(config, "dwell"):
        rules.append(
            DwellRule(
                zone_id=_require_id(spec, "zone_id", "dwell"),
                polygon=spec.get("polygon"),
                seconds=spec.get("seconds"),
                severity=_severity(spec),
                event_type=spec.get("event_type", "dwell"),
            )
        )
    for spec in _rule_specs(config, "count"):
        rules.append(
            CountRule(
                zone_id=_require_id(spec, "zone_id", "count"),
                polygon=spec.get("polygon"),
                threshold=spec.get("threshold"),
                consecutive_frames=spec.get("consecutive_frames", 1),
                severity=_severity(spec),
                event_type=spec.get("event_type", "count"),
            )
        )
    return rules
