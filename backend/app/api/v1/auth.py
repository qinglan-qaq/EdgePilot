from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import _bearer, get_current_user
from app.core.exceptions import Unauthorized
from app.db import get_db
from app.models.user import User
from app.schemas.auth import LoginRequest, RefreshRequest
from app.schemas.common import ok
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db),
) -> dict:
    pair = await AuthService(session).login(
        payload.email,
        payload.password,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return ok(pair.model_dump())


@router.post("/refresh")
async def refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_db),
) -> dict:
    pair = await AuthService(session).refresh(payload.refresh_token)
    return ok(pair.model_dump())


@router.post("/logout")
async def logout(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> dict:
    if creds is None:
        raise Unauthorized("缺少令牌")
    await AuthService(session).logout(creds.credentials)
    return ok(None)


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    return ok(
        {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
            "tenant_id": str(user.tenant_id),
        }
    )
