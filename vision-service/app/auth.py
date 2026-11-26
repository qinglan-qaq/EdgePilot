from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request, status

from .config import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def require_token(
    request: Request,
    x_vision_token: str | None = Header(default=None, alias="X-Vision-Token"),
    settings: Settings = Depends(get_settings),
) -> None:
    """Optional shared-secret guard.

    An empty configured token disables auth for local development; when a token
    is configured the comparison is constant time.
    """

    if not settings.token:
        return
    provided = x_vision_token or ""
    if not hmac.compare_digest(provided.encode("utf-8"), settings.token.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid vision token")
