"""几何工具：射线法多边形判定、bbox 中心、线段真实相交。

坐标约定：``BBox = (x, y, w, h)``，与 ``vision_events.bbox`` 的 JSON 形状一致。
所有函数只做纯计算，不依赖 numpy。
"""

from __future__ import annotations

from collections.abc import Sequence

Point = tuple[float, float]
BBox = tuple[float, float, float, float]
Polygon = Sequence[Point]

# 边界/共线判定容差；视觉坐标量级远大于该值。
EPS = 1e-9


def bbox_center(bbox: BBox) -> Point:
    """返回 ``(x, y, w, h)`` bbox 的中心点。"""
    x, y, w, h = bbox
    return (float(x) + float(w) / 2.0, float(y) + float(h) / 2.0)


def _on_segment(point: Point, start: Point, end: Point) -> bool:
    px, py = point
    ax, ay = start
    bx, by = end
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    scale = max(1.0, abs(bx - ax) + abs(by - ay))
    if abs(cross) > EPS * scale:
        return False
    return (
        min(ax, bx) - EPS <= px <= max(ax, bx) + EPS
        and min(ay, by) - EPS <= py <= max(ay, by) + EPS
    )


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """射线法判断点是否在多边形内。

    点恰好落在任意边或顶点上时稳定返回 ``True``（视为在内），避免边界抖动。
    """
    pts = [(float(x), float(y)) for x, y in polygon]
    if len(pts) < 3:
        return False

    px, py = point
    inside = False
    count = len(pts)
    for index in range(count):
        start = pts[index]
        end = pts[(index + 1) % count]
        if _on_segment((px, py), start, end):
            return True
        (ax, ay), (bx, by) = start, end
        if (ay > py) != (by > py):
            intersect_x = ax + (py - ay) * (bx - ax) / (by - ay)
            if intersect_x > px:
                inside = not inside
    return inside


def cross_sign(origin: Point, first: Point, second: Point) -> int:
    """叉积符号：``cross(first-origin, second-origin)`` 的 -1/0/1。

    共线（含端点）返回 0，调用方应显式处理，不得据此编造方向。
    """
    (ox, oy), (ax, ay), (bx, by) = origin, first, second
    value = (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)
    if abs(value) <= EPS:
        return 0
    return 1 if value > 0 else -1


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """线段 p1-p2 与 q1-q2 是否真实相交（含端点接触与共线重叠）。"""
    d1 = cross_sign(q1, q2, p1)
    d2 = cross_sign(q1, q2, p2)
    d3 = cross_sign(p1, p2, q1)
    d4 = cross_sign(p1, p2, q2)

    if d1 * d2 < 0 and d3 * d4 < 0:
        return True

    # 共线/端点接触的退化情形。
    if d1 == 0 and _on_segment(p1, q1, q2):
        return True
    if d2 == 0 and _on_segment(p2, q1, q2):
        return True
    if d3 == 0 and _on_segment(q1, p1, p2):
        return True
    if d4 == 0 and _on_segment(q2, p1, p2):
        return True
    return False
