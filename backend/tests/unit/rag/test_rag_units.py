"""RAG 纯函数单元测试：解析、分块、RRF。"""

from __future__ import annotations

import pytest

from app.rag.chunking import chunk_document
from app.rag.errors import DocumentEmptyError, UnsupportedDocumentType
from app.rag.parsing import parse_document, parse_text
from app.rag.retriever import rrf_fuse

LONG_TEXT = "消防通道不得堆放杂物，巡检人员需要定期检查疏散指示与应急照明。" * 80


def test_parse_markdown_and_text():
    md = parse_document("policy.md", "# 标题\n\n消防通道管理。".encode("utf-8"))
    assert md.suffix == ".md"
    assert "消防通道" in md.text
    assert md.title_hint == "标题"

    txt = parse_document("notes.txt", "纯文本内容\n第二行".encode("utf-8"))
    assert txt.suffix == ".txt"
    assert "第二行" in txt.text

    direct = parse_text("abc", suffix=".markdown")
    assert direct.text == "abc"


def test_parse_pdf_uses_pymupdf():
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Fire escape corridor inspection policy")
    data = doc.tobytes()
    doc.close()

    parsed = parse_document("policy.pdf", data)
    assert parsed.suffix == ".pdf"
    assert parsed.pages == 1
    assert "Fire escape" in parsed.text


def test_parse_rejects_unsupported_empty_and_bad_pdf():
    with pytest.raises(UnsupportedDocumentType) as exc:
        parse_document("report.docx", b"whatever")
    assert "python-docx" in str(exc.value)

    with pytest.raises(DocumentEmptyError):
        parse_document("empty.txt", b"")

    with pytest.raises(DocumentEmptyError):
        parse_document("blank.md", b"   \n\t  ")

    with pytest.raises(Exception) as exc:
        parse_document("broken.pdf", b"not a pdf")
    assert "PDF" in str(exc.value)


def test_chunking_ranges_no_empty_and_overlap():
    chunks = chunk_document(LONG_TEXT, target=500, min_size=400, max_size=600, overlap=80)
    assert len(chunks) >= 5
    assert [chunk.chunk_no for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.content.strip() for chunk in chunks)
    assert all(len(chunk.content) <= 600 for chunk in chunks)
    assert all(len(chunk.content) >= 400 for chunk in chunks[:-1])
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start == previous.end - 80
        assert current.start < previous.end
    # 块内容确实来自对应原文区间
    for chunk in chunks:
        assert LONG_TEXT[chunk.start:chunk.end].strip() == chunk.content


def test_chunking_keeps_heading_path():
    text = (
        "# 总则\n\n"
        + "这是总则部分的说明文字。" * 40
        + "\n\n## 消防通道\n\n"
        + "消防通道不得占用，需要保持畅通。" * 40
        + "\n"
    )
    chunks = chunk_document(text)
    headings = {chunk.heading for chunk in chunks if chunk.heading}
    assert any("总则" in heading for heading in headings)
    assert any("消防通道" in heading for heading in headings)


def test_rrf_fuse_is_pure_and_order_stable():
    legs = {
        "fts": [(1, 0.9), (2, 0.5), (3, 0.1)],
        "trigram": [(2, 0.8), (4, 0.2)],
    }
    fused = rrf_fuse(legs, k=60)
    assert [item.chunk_id for item in fused] == [2, 1, 4, 3]
    by_id = {item.chunk_id: item for item in fused}
    assert by_id[2].rrf == pytest.approx(1 / 62 + 1 / 61)
    assert by_id[1].rrf == pytest.approx(1 / 61)
    assert by_id[2].scores == {"fts": 0.5, "trigram": 0.8}
    assert by_id[3].scores.get("trigram") is None
    # 输入不被修改
    assert legs["fts"][0] == (1, 0.9)


def test_rrf_rejects_bad_k():
    with pytest.raises(ValueError):
        rrf_fuse({"fts": [(1, 1.0)]}, k=0)
