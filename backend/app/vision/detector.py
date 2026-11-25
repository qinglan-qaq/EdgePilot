"""检测器抽象。

``Detection.bbox`` 统一为 ``(x, y, w, h)``。``MockDetector`` 只回放显式配置的
轨迹（确定性、可复现）；没有轨迹时返回空列表，绝不随机生成或编造目标。
``YoloDetector`` 仅保留骨架，本环境不安装 ultralytics。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Detection:
    label: str
    bbox: BBox
    confidence: float
    track_id: int | None = None


@runtime_checkable
class Detector(Protocol):
    model_version: str

    def detect(self, frame: Any) -> list[Detection]: ...


class MockDetector:
    """确定性、可脚本化的检测器。

    ``frames[i]`` 是第 i 次 ``detect`` 调用的返回值；调用次数超过脚本长度时
    返回空列表（``loop=True`` 时循环）。``frame`` 参数可为任意对象，内部只用
    帧计数器，不读取像素、不依赖 numpy。
    """

    is_mock = True
    model_version = "mock-v1"

    def __init__(
        self,
        frames: Sequence[Sequence[Detection]] | None = None,
        *,
        loop: bool = False,
    ) -> None:
        self._frames = [list(item) for item in (frames or [])]
        self._loop = loop
        self.frame_index = 0

    def detect(self, frame: Any = None) -> list[Detection]:
        index = self.frame_index
        self.frame_index += 1
        if not self._frames:
            return []
        if self._loop:
            index %= len(self._frames)
        if index >= len(self._frames):
            return []
        return list(self._frames[index])


class YoloDetector:
    """YOLO 接入骨架：本期不安装 ultralytics，构造即显式失败。"""

    model_version = "yolo"

    _MESSAGE = "接入 ultralytics 后实现；本期不安装"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(self._MESSAGE)

    def detect(self, frame: Any) -> list[Detection]:  # pragma: no cover - 构造已拦截
        raise NotImplementedError(self._MESSAGE)


def create_detector(config: dict | None) -> Detector:
    """按 ``devices.config.detector`` 选择检测器；未知类型抛 ValueError。"""
    config = config or {}
    kind = config.get("detector", "mock")
    if kind == "mock":
        mock_config = config.get("mock") or {}
        frames = config.get("mock_frames") or mock_config.get("frames") or []
        return MockDetector(frames)
    if kind == "yolo":
        # 构造本身即 NotImplementedError，保持与骨架一致的显式失败语义。
        return YoloDetector()
    raise ValueError(f"未知 detector 类型: {kind!r}（可选 mock / yolo）")
