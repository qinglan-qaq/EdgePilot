import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.schemas.auth import TokenPayload

_ph = PasswordHasher()


def hash_password(raw: str) -> str:
    return _ph.hash(raw)


def verify_password(hashed: str, raw: str) -> bool:
    try:
        return _ph.verify(hashed, raw)
    except VerifyMismatchError:
        return False


def encode_payload(payload: TokenPayload) -> str:
    s = get_settings()
    return jwt.encode(payload.model_dump(), s.jwt_secret, algorithm=s.jwt_algorithm)


def create_token(sub: str, tenant_id: str, role: str, kind: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    ttl = timedelta(minutes=s.access_token_minutes) if kind == "access" else timedelta(days=s.refresh_token_days)
    return encode_payload(TokenPayload(sub=sub, tid=tenant_id, role=role, typ=kind,
                                       iat=now, exp=now + ttl, jti=secrets.token_hex(8)))


def decode_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
