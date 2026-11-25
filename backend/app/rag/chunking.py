"""标题层级 + 递归分块。

目标块长 500（允许 400–600），相邻块保留 80 字符重叠。分块先按 Markdown 标题
切成 section，再在每个 section 内用窗口滑动；窗口优先在段落/句末断开，找不到
边界时退化为固定目标长度。``Chunk`` 同时携带原文偏移，便于测试精确验证重叠。

最后一块允许短于下限（文档尾部自然长度），但绝不产生空块，且 ``chunk_no`` 从 0
连续递增。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+([^\n]+)$", re.MULTILINE)
_BOUNDARIES = "\n。！？；.!?;"

DEFAULT_TARGET = 500
DEFAULT_MIN = 400
DEFAULT_MAX = 600
DEFAULT_OVERLAP = 80


@dataclass(frozen=True)
class Chunk:
    chunk_no: int
    content: str
    start: int
    end: int
    heading: str | None = None


@dataclass(frozen=True)
class _Section:
    start: int
    end: int
    heading: str | None


def _normalize(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # 超过 3 个连续换行压缩成 2 个，避免空 section 和块内大段空白
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip("\n")


def _sections(text: str) -> list[_Section]:
    matches = list(HEADING_RE.finditer(text))
    if not matches:
        return [_Section(0, len(text), None)]

    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []

    def heading_path(level: int, title: str) -> str:
        stack[:] = [(lvl, name) for lvl, name in stack if lvl < level]
        stack.append((level, title))
        return " / ".join(name for _, name in stack)

    if matches[0].start() > 0:
        prefix = text[: matches[0].start()]
        if prefix.strip():
            sections.append(_Section(0, matches[0].start(), None))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        level = len(match.group(1))
        path = heading_path(level, match.group(2).strip())
        sections.append(_Section(match.start(), end, path))
    return sections


def _boundary_end(text: str, start: int, lo: int, hi: int, target_end: int) -> int:
    """在 [lo, hi) 内找离 target_end 最近的句/段边界，返回断点之后的下标。"""
    limit = min(target_end, hi)
    for index in range(limit, lo - 1, -1):
        if index - 1 >= lo and text[index - 1] in _BOUNDARIES:
            return index
    return min(target_end, hi)


def _blocks(text: str, *, min_size: int, max_size: int) -> list[_Section]:
    """把相邻的小 section 合并成至少 min_size 的块，避免标题密集的文档切出碎块。

    单个 section 超过块长时保持独立，由窗口逻辑在其内部递归切分。
    """
    blocks: list[_Section] = []
    start: int | None = None
    end = 0
    heading: str | None = None
    for section in _sections(text):
        if start is None:
            start, end, heading = section.start, section.end, section.heading
            continue
        if end - start < min_size:
            end = section.end  # 小段合并，直到接近目标下限
            continue
        blocks.append(_Section(start, end, heading))
        start, end, heading = section.start, section.end, section.heading
    if start is not None:
        blocks.append(_Section(start, end, heading))

    # 尾部碎块若能与前一块一起塞进 max_size，则并入前一块。
    if len(blocks) >= 2:
        last = blocks[-1]
        if last.end - last.start < min_size:
            previous = blocks[-2]
            if last.end - previous.start <= max_size:
                blocks[-2] = _Section(previous.start, last.end, previous.heading)
                blocks.pop()
    return blocks


def chunk_document(
    text: str,
    *,
    target: int = DEFAULT_TARGET,
    min_size: int = DEFAULT_MIN,
    max_size: int = DEFAULT_MAX,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """把文本切成带偏移的块；空文本返回空列表。"""
    normalized = _normalize(text)
    if not normalized:
        return []
    if not (0 < min_size <= target <= max_size):
        raise ValueError("分块参数必须满足 0 < min_size <= target <= max_size")
    if overlap < 0 or overlap >= min_size:
        raise ValueError("overlap 必须 >= 0 且小于 min_size")

    chunks: list[Chunk] = []
    for section in _blocks(normalized, min_size=min_size, max_size=max_size):
        start = section.start
        while start < section.end:
            remaining = section.end - start
            if remaining <= max_size:
                end = section.end
            else:
                lo = start + min_size
                hi = min(start + max_size, section.end)
                if lo > hi:
                    lo = hi
                end = _boundary_end(
                    normalized, start, lo, hi, min(start + target, section.end)
                )
            content = normalized[start:end].strip()
            if content:
                chunks.append(
                    Chunk(
                        chunk_no=len(chunks),
                        content=content,
                        start=start,
                        end=end,
                        heading=section.heading,
                    )
                )
            if end >= section.end:
                break
            start = end - overlap
            if start <= chunks[-1].start:  # 防御：极端参数下绝不原地打转
                start = chunks[-1].start + 1
    return chunks


def chunk_text(text: str, **kwargs) -> list[str]:
    """只要文本内容的便捷入口。"""
    return [chunk.content for chunk in chunk_document(text, **kwargs)]
