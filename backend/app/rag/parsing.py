"""文档解析：PDF / Markdown / 纯文本。

- ``.pdf`` 使用 PyMuPDF（``import pymupdf``）逐页抽取文本；
- ``.md`` / ``.markdown`` / ``.txt`` 按 UTF-8 解码（容忍 BOM）；
- ``.docx`` 本机没有 python-docx，明确抛 ``UnsupportedDocumentType``，不静默吞掉；
- 解析结果为空时抛 ``DocumentEmptyError``，绝不写入半截数据。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.rag.errors import DocumentEmptyError, DocumentParseError, UnsupportedDocumentType

_TEXT_SUFFIXES = {".md", ".markdown", ".txt"}


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    suffix: str
    title_hint: str | None = None
    pages: int | None = None


def safe_filename(filename: str | None, *, fallback: str = "upload") -> str:
    """只保留文件名本身，去掉目录、NUL 与控制字符，限制长度。"""
    raw = (filename or "").replace("\x00", "").replace("\\", "/")
    name = Path(raw).name.strip()
    # 去掉可能用于日志伪造的换行/控制字符
    name = "".join(ch for ch in name if ch.isprintable())
    name = name[:255].strip()
    return name or fallback


def _decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentParseError("文本文件不是合法的 UTF-8 编码") from exc


def _parse_pdf(data: bytes) -> tuple[str, int]:
    try:
        import pymupdf  # noqa: PLC0415 - 可选依赖，延迟导入
    except ImportError as exc:  # pragma: no cover - 本机已安装
        raise DocumentParseError("PDF 解析依赖 pymupdf 未安装") from exc

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - 任何 PDF 损坏都转业务错误
        raise DocumentParseError("PDF 文件损坏或无法读取") from exc

    try:
        pages = doc.page_count
        parts: list[str] = []
        for page in doc:
            parts.append(page.get_text("text"))
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError("PDF 文本抽取失败") from exc
    finally:
        doc.close()
    return "\n".join(parts), pages


def _title_hint(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip() or None
        return line[:120]
    return None


def parse_text(text: str, *, suffix: str = ".txt", title_hint: str | None = None) -> ParsedDocument:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise DocumentEmptyError()
    return ParsedDocument(
        text=normalized,
        suffix=suffix.lower(),
        title_hint=title_hint or _title_hint(normalized),
        pages=None,
    )


def parse_document(filename: str, data: bytes) -> ParsedDocument:
    """按扩展名解析上传内容；不支持/空内容/损坏分别给出明确错误。"""
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if not data:
        raise DocumentEmptyError("上传文件为空，未写入知识库")

    if suffix in _TEXT_SUFFIXES:
        text = _decode_text(data)
        return parse_text(text, suffix=suffix)
    if suffix == ".pdf":
        text, pages = _parse_pdf(data)
        if not text.strip():
            raise DocumentParseError("PDF 没有可抽取的文本（可能是扫描件；本期不做 OCR）")
        parsed = parse_text(text, suffix=suffix)
        return ParsedDocument(
            text=parsed.text,
            suffix=suffix,
            title_hint=parsed.title_hint,
            pages=pages,
        )
    raise UnsupportedDocumentType(suffix)
