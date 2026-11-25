from datetime import datetime

from pydantic import BaseModel


class TokenPayload(BaseModel):
    sub: str
    tid: str
    role: str
    typ: str  # access | refresh
    iat: datetime
    exp: datetime
    jti: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
