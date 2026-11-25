"""知识库入库、混合检索、API 权限与种子幂等集成测试（Task 20）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.knowledge import KnowledgeChunk
from app.models.user import User
from app.rag.service import RagService
from app.tools.builtin import register_builtins
from app.tools.builtin.knowledge import search_knowledge_base
from app.tools.registry import ToolContext, ToolRegistry
from scripts.seed_demo import seed_all
from tests.conftest import (
    OTHER_TENANT,
    PASSWORD,
    TENANT,
    auth_headers,
    login_token,
)

DOC_FIRE = (
    "# 消防通道管理规范\n\n"
    + "消防通道不得堆放杂物，巡检人员需要检查疏散指示和应急照明。" * 60
    + "\n\n## 处置流程\n\n"
    + "发现消防通道被占用时，应立即通知责任人清理并记录处理结果。" * 60
    + "\n"
)

DOC_CROWD = (
    "# 人群聚集处置指引\n\n"
    + "人群聚集时应评估密度、通行速度和跌倒风险，必要时启动分流预案。" * 80
    + "\n"
)


async def _seed_doc(session, *, tenant_id=TENANT, filename="fire.md", text=DOC_FIRE,
                    title="消防通道管理规范"):
    return await RagService(session).ingest_text(
        tenant_id=tenant_id, filename=filename, text=text, title=title, commit=False
    )


def _upload(client, token, filename: str, data: bytes, title: str | None = None):
    form = {} if title is None else {"title": title}
    return client.post(
        "/api/v1/knowledge/documents",
        files={"file": (filename, data, "application/octet-stream")},
        data=form,
        headers=auth_headers(token),
    )


async def test_upload_list_search_and_tenant_isolation(
    client, session, admin_token, viewer_token, other_tenant_token, seeded_users
):
    response = await _upload(client, admin_token, "fire.md", DOC_FIRE.encode("utf-8"),
                            title="消防通道管理规范")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["created"] is True
    assert body["duplicate"] is False
    assert body["chunk_count"] >= 1
    assert body["embedding_available"] is False
    assert body["embedding_model"] is None

    listing = (await client.get("/api/v1/knowledge/documents",
                                headers=auth_headers(viewer_token))).json()["data"]
    assert listing["total"] == 1
    assert listing["items"][0]["doc_id"] == body["doc_id"]
    assert listing["items"][0]["chunk_count"] == body["chunk_count"]

    search = await client.post(
        "/api/v1/knowledge/search",
        json={"query": "消防通道", "top_k": 3},
        headers=auth_headers(viewer_token),
    )
    assert search.status_code == 200
    data = search.json()["data"]
    assert data["results"], "词面命中应返回真实 chunk"
    assert data["empty_reason"] is None
    assert len(data["results"]) <= 3
    first = data["results"][0]
    assert isinstance(first["chunk_id"], int)
    assert first["source"] == "fire.md"
    assert first["score"] is not None
    assert len(first["snippet"]) <= 301
    assert set(first["scores"]) == {"vector", "fts", "trigram", "rrf", "rerank"}
    assert first["scores"]["rrf"] is not None
    assert first["scores"]["vector"] is None  # 没有 embedding，不伪造
    assert first["scores"]["rerank"] is None
    assert data["vector_search_unavailable"] is True
    assert "vector_unavailable" in data["degraded"]
    assert "reranker_unavailable" in data["degraded"]
    assert data["embedding_model"] is None
    assert data["reranker_model"] is None

    # 另一个租户看不到任何文档，检索返回空 + knowledge_base_empty
    other_list = (await client.get("/api/v1/knowledge/documents",
                                   headers=auth_headers(other_tenant_token))).json()["data"]
    assert other_list["total"] == 0
    other_search = (
        await client.post(
            "/api/v1/knowledge/search",
            json={"query": "消防通道"},
            headers=auth_headers(other_tenant_token),
        )
    ).json()["data"]
    assert other_search["results"] == []
    assert other_search["empty_reason"] == "knowledge_base_empty"
    assert other_search["note"]


async def test_duplicate_upload_is_idempotent(client, session, admin_token, seeded_users):
    first = await _upload(client, admin_token, "fire.md", DOC_FIRE.encode("utf-8"))
    second = await _upload(client, admin_token, "fire.md", DOC_FIRE.encode("utf-8"))
    assert first.status_code == second.status_code == 200
    a, b = first.json()["data"], second.json()["data"]
    assert a["doc_id"] == b["doc_id"]
    assert a["created"] is True and b["created"] is False
    assert b["duplicate"] is True
    assert a["chunk_count"] == b["chunk_count"]

    count = len(
        (
            await session.scalars(
                select(KnowledgeChunk).where(
                    KnowledgeChunk.tenant_id == TENANT,
                    KnowledgeChunk.doc_id == a["doc_id"],
                )
            )
        ).all()
    )
    assert count == a["chunk_count"]


async def test_upload_errors_are_explicit(client, admin_token, seeded_users):
    empty = await _upload(client, admin_token, "empty.txt", b"")
    assert empty.status_code == 400
    assert "为空" in empty.json()["message"]

    docx = await _upload(client, admin_token, "report.docx", b"fake-docx")
    assert docx.status_code == 400
    assert "python-docx" in docx.json()["message"]

    unsupported = await _upload(client, admin_token, "archive.zip", b"PK")
    assert unsupported.status_code == 400
    assert "不支持" in unsupported.json()["message"]

    blank = await _upload(client, admin_token, "blank.md", b"   \n\t")
    assert blank.status_code == 400

    too_large = await _upload(
        client, admin_token, "big.txt", b"a" * (10 * 1024 * 1024 + 1)
    )
    assert too_large.status_code == 400
    assert "10MB" in too_large.json()["message"]

    # 路径型文件名只取 basename，不写盘、不保留目录部分
    unsafe = await _upload(client, admin_token, "../../evil.md", DOC_CROWD.encode("utf-8"))
    assert unsafe.status_code == 200
    assert unsafe.json()["data"]["source"] == "evil.md"


async def test_permissions_and_cross_tenant_delete(
    client, session, admin_token, viewer_token, other_tenant_token, seeded_users
):
    # viewer 可 search/list，但上传/删除 403
    denied_upload = await _upload(client, viewer_token, "viewer.md", DOC_CROWD.encode("utf-8"))
    assert denied_upload.status_code == 403
    viewer_search = await client.post(
        "/api/v1/knowledge/search", json={"query": "行人"},
        headers=auth_headers(viewer_token),
    )
    assert viewer_search.status_code == 200
    viewer_list = await client.get(
        "/api/v1/knowledge/documents", headers=auth_headers(viewer_token)
    )
    assert viewer_list.status_code == 200

    # operator 可上传/删除
    operator = User(
        tenant_id=TENANT,
        email="operator@edgepilot.local",
        password_hash=hash_password(PASSWORD),
        display_name="Operator",
        role="operator",
        status="active",
    )
    session.add(operator)
    await session.flush()
    operator_token = await login_token(client, operator.email)

    uploaded = await _upload(client, operator_token, "fire.md", DOC_FIRE.encode("utf-8"))
    assert uploaded.status_code == 200
    doc_id = uploaded.json()["data"]["doc_id"]

    denied_delete = await client.delete(
        f"/api/v1/knowledge/documents/{doc_id}", headers=auth_headers(viewer_token)
    )
    assert denied_delete.status_code == 403

    # 跨租户删除 404
    cross = await client.delete(
        f"/api/v1/knowledge/documents/{doc_id}", headers=auth_headers(other_tenant_token)
    )
    assert cross.status_code == 404

    deleted = await client.delete(
        f"/api/v1/knowledge/documents/{doc_id}", headers=auth_headers(operator_token)
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted_chunks"] >= 1

    again = await client.delete(
        f"/api/v1/knowledge/documents/{doc_id}", headers=auth_headers(operator_token)
    )
    assert again.status_code == 404

    audit_actions = set(
        (
            await session.scalars(
                select(AuditLog.action).where(
                    AuditLog.action.in_(["knowledge.upload", "knowledge.delete"])
                )
            )
        ).all()
    )
    assert {"knowledge.upload", "knowledge.delete"} <= audit_actions


async def test_empty_and_no_match(client, session, admin_token, other_tenant_token, seeded_users):
    empty = (
        await client.post(
            "/api/v1/knowledge/search",
            json={"query": "消防通道"},
            headers=auth_headers(other_tenant_token),
        )
    ).json()["data"]
    assert empty["results"] == []
    assert empty["empty_reason"] == "knowledge_base_empty"

    await _seed_doc(session)
    await session.flush()
    no_match = (
        await client.post(
            "/api/v1/knowledge/search",
            json={"query": "zzzzzz-no-such-token"},
            headers=auth_headers(admin_token),
        )
    ).json()["data"]
    assert no_match["results"] == []
    assert no_match["empty_reason"] == "no_match"
    assert no_match["note"]


async def test_top_k_validation(client, session, admin_token, seeded_users):
    await _seed_doc(session, text=DOC_FIRE)
    await session.flush()
    too_large = await client.post(
        "/api/v1/knowledge/search", json={"query": "消防通道", "top_k": 21},
        headers=auth_headers(admin_token),
    )
    assert too_large.status_code == 422
    too_small = await client.post(
        "/api/v1/knowledge/search", json={"query": "消防通道", "top_k": 0},
        headers=auth_headers(admin_token),
    )
    assert too_small.status_code == 422


class FakeEmbedder:
    available = True
    model_name = "test-embedder"
    dimension = 1024

    def __init__(self, vector: list[float] | None = None):
        self.vector = vector or ([1.0] + [0.0] * 1023)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


class FakeReranker:
    available = True
    model_name = "test-reranker"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [float(len(documents) - index) for index in range(len(documents))]


async def test_vector_leg_with_handwritten_embedding(session):
    result = await _seed_doc(session)
    assert result.embedding_available is False
    chunks = (
        await session.scalars(
            select(KnowledgeChunk).where(
                KnowledgeChunk.tenant_id == TENANT,
                KnowledgeChunk.doc_id == result.doc_id,
            )
        )
    ).all()
    assert chunks and all(chunk.embedding is None for chunk in chunks)
    assert all(chunk.meta["embedding_available"] is False for chunk in chunks)

    # 手工写入 1024 维向量，仅用于验证 cosine 腿；生产没有 provider 时不会这样做。
    vector = [1.0] + [0.0] * 1023
    for chunk in chunks:
        chunk.embedding = vector
    await session.flush()

    outcome = await RagService(session, embedder=FakeEmbedder(vector)).search(
        tenant_id=TENANT, query="消防通道", top_k=5
    )
    assert outcome.results
    assert outcome.vector_search_unavailable is False
    assert outcome.embedding_model == "test-embedder"
    for item in outcome.results:
        assert item["scores"]["vector"] is not None
        assert item["scores"]["vector"] == pytest.approx(1.0, abs=1e-6)
        assert item["score"] is not None


async def test_reranker_available_uses_real_scores(session):
    await _seed_doc(session)
    await session.flush()
    default = await RagService(session).search(tenant_id=TENANT, query="消防通道", top_k=3)
    assert "reranker_unavailable" in default.degraded
    assert default.rerank_unavailable is True
    assert all(item["scores"]["rerank"] is None for item in default.results)

    reranked = await RagService(session, reranker=FakeReranker()).search(
        tenant_id=TENANT, query="消防通道", top_k=3
    )
    assert reranked.strategy == "hybrid_rrf_rerank"
    assert reranked.rerank_unavailable is False
    assert reranked.reranker_model == "test-reranker"
    assert all(item["scores"]["rerank"] is not None for item in reranked.results)
    rerank_scores = [item["scores"]["rerank"] for item in reranked.results]
    assert rerank_scores == sorted(rerank_scores, reverse=True)


async def test_tool_reuses_hybrid_retrieval(session):
    await _seed_doc(session)
    await session.flush()
    payload = await search_knowledge_base(
        {"query": "消防通道", "top_k": 3},
        ToolContext(session=session, tenant_id=TENANT),
    )
    assert payload["count"] >= 1
    item = payload["items"][0]
    assert {"chunk_id", "source", "snippet", "score", "scores"} <= set(item)
    assert item["score"] is not None
    assert payload["empty_reason"] is None

    empty = await search_knowledge_base(
        {"query": "消防通道"},
        ToolContext(session=session, tenant_id=OTHER_TENANT),
    )
    assert empty["items"] == []
    assert empty["count"] == 0
    assert empty["empty_reason"] == "knowledge_base_empty"
    assert empty["note"]

    # 通过 ToolRegistry 执行，验证 output_schema 仍接受新字段
    registry = ToolRegistry()
    register_builtins(registry)
    result = await registry.execute(
        "search_knowledge_base",
        {"query": "消防通道", "top_k": 2},
        ToolContext(session=session, tenant_id=TENANT),
    )
    assert result.ok
    assert result.data["count"] >= 1
    assert result.data["items"][0]["chunk_id"] is not None


async def test_fts_leg_scores_english_tokens(session):
    text = "fire escape corridor inspection policy requires a clear passage. " * 60
    await RagService(session).ingest_text(
        tenant_id=TENANT, filename="en.md", text=text, title="Corridor policy", commit=False
    )
    await session.flush()
    outcome = await RagService(session).search(
        tenant_id=TENANT, query="corridor inspection", top_k=5
    )
    assert outcome.results
    assert any(item["scores"]["fts"] is not None for item in outcome.results)


async def test_seed_is_idempotent_and_searchable(session, tmp_path):
    now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)
    first = await seed_all(session, tenant_id=TENANT, storage_dir=tmp_path, now=now)
    second = await seed_all(session, tenant_id=TENANT, storage_dir=tmp_path, now=now)

    assert first["nodes"] == 2
    assert first["devices"] == 4
    assert first["events_total"] == 200
    assert first["seed_events"] == 200
    assert first["snapshot_paths"] == 20
    assert first["snapshots_written"] == 20
    assert first["docs"] == 3
    assert first["chunks"] >= 3

    assert second["events_total"] == 200
    assert second["seed_events"] == 200
    assert second["events_created"] == 0
    assert second["snapshots_written"] == 0
    assert second["docs"] == 3
    assert second["docs_created"] == 0
    assert second["chunks"] == first["chunks"]
    assert second["nodes"] == 2
    assert second["devices"] == 4

    snapshot_files = list((tmp_path / "snapshots" / "events").rglob("*.png"))
    assert len(snapshot_files) == 20

    # 跨天重跑不按新日期新建目录；缺失文件只按库内旧路径补写。
    third = await seed_all(
        session, tenant_id=TENANT, storage_dir=tmp_path, now=now + timedelta(days=1)
    )
    assert third["events_created"] == 0
    assert third["snapshots_written"] == 0
    assert len(list((tmp_path / "snapshots" / "events").rglob("*.png"))) == 20

    missing = snapshot_files[0]
    missing.unlink()
    fourth = await seed_all(
        session, tenant_id=TENANT, storage_dir=tmp_path, now=now + timedelta(days=2)
    )
    assert fourth["snapshots_written"] == 1
    assert missing.is_file()
    assert len(list((tmp_path / "snapshots" / "events").rglob("*.png"))) == 20

    chunks = (
        await session.scalars(
            select(KnowledgeChunk).where(KnowledgeChunk.tenant_id == TENANT)
        )
    ).all()
    assert chunks and all(chunk.embedding is None for chunk in chunks)

    outcome = await RagService(session).search(
        tenant_id=TENANT, query="消防通道", top_k=5
    )
    assert outcome.results
    assert any("消防通道" in item["snippet"] for item in outcome.results)

    crowd = await RagService(session).search(
        tenant_id=TENANT, query="人群聚集", top_k=5
    )
    assert crowd.results
    assert any("人群" in item["snippet"] for item in crowd.results)
