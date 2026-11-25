"""知识库 API：上传/列表/删除/混合检索。

权限：viewer 可 list / search；上传与删除仅 admin/operator（其余 403）。
所有查询与删除都带 tenant_id；跨租户删除统一 404。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.exceptions import AppError
from app.core.pagination import PageParams, PageResult
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.knowledge import (
    KnowledgeDeleteOut,
    KnowledgeDocumentOut,
    KnowledgeSearchRequest,
    KnowledgeUploadOut,
)
from app.services.knowledge_service import KnowledgeService

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_CHUNK_READ = 1024 * 1024


async def _read_limited(file: UploadFile) -> bytes:
    """分块读取并限制 10MB；空文件显式 400。"""
    buffer = bytearray()
    while True:
        piece = await file.read(_CHUNK_READ)
        if not piece:
            break
        buffer.extend(piece)
        if len(buffer) > MAX_UPLOAD_BYTES:
            raise AppError("文件超过 10MB 上传上限")
    if not buffer:
        raise AppError("上传文件为空")
    return bytes(buffer)


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    ip = request.client.host if request.client else None
    return ip, request.headers.get("user-agent")


@router.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    ip, user_agent = _client_meta(request)
    filename = file.filename
    try:
        data = await _read_limited(file)
    finally:
        await file.close()

    result = await KnowledgeService(session).upload(
        tenant_id=user.tenant_id,
        filename=filename,
        data=data,
        title=title,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    return ok(KnowledgeUploadOut(**result).model_dump())


@router.get("/documents")
async def list_documents(
    params: PageParams = Depends(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    items, total = await KnowledgeService(session).list_documents(
        user.tenant_id, page=params.page, size=params.size
    )
    page = PageResult[KnowledgeDocumentOut](
        items=[KnowledgeDocumentOut(**item) for item in items],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    request: Request,
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    ip, user_agent = _client_meta(request)
    result = await KnowledgeService(session).delete(
        tenant_id=user.tenant_id,
        doc_id=doc_id,
        user=user,
        ip=ip,
        user_agent=user_agent,
    )
    return ok(KnowledgeDeleteOut(**result).model_dump())


@router.post("/search")
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    outcome = await KnowledgeService(session).search(
        tenant_id=user.tenant_id,
        query=payload.query,
        top_k=payload.top_k,
    )
    return ok(outcome.to_dict())
