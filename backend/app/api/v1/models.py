"""模型配置 CRUD 路由：登录即可读，只有 admin 可写。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_role
from app.core.pagination import PageParams, PageResult
from app.core.tenancy import get_tenant_id
from app.db import get_db
from app.models.user import User
from app.schemas.common import ok
from app.schemas.model import ModelCreate, ModelOut, ModelUpdate
from app.services.model_service import ModelService

router = APIRouter(prefix="/models", tags=["models"])


@router.get("")
async def list_models(
    params: PageParams = Depends(),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    rows, total = await ModelService(session).list(params.page, params.size)
    page = PageResult[ModelOut](
        items=[ModelOut.model_validate(row) for row in rows],
        total=total,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())


@router.post("")
async def create_model(
    payload: ModelCreate,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await ModelService(session).create(get_tenant_id(), **payload.model_dump())
    return ok(ModelOut.model_validate(row).model_dump())


@router.get("/{model_id}")
async def get_model(
    model_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await ModelService(session).get(model_id)
    return ok(ModelOut.model_validate(row).model_dump())


@router.patch("/{model_id}")
async def update_model(
    model_id: int,
    payload: ModelUpdate,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    row = await ModelService(session).update(
        model_id, **payload.model_dump(exclude_unset=True)
    )
    return ok(ModelOut.model_validate(row).model_dump())


@router.delete("/{model_id}")
async def delete_model(
    model_id: int,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    await ModelService(session).delete(model_id)
    return ok(None)


@router.post("/{model_id}/test")
async def test_model_connection(
    model_id: int,
    _: User = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    return ok(await ModelService(session).test_connection(model_id))
