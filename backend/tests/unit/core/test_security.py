import pytest

from app.core.security import create_token, decode_token, hash_password, verify_password


def test_password_roundtrip():
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert verify_password(h, "s3cret-pass") is True
    assert verify_password(h, "wrong") is False


def test_token_roundtrip_and_type_claim():
    token = create_token(sub="1", tenant_id="t-1", role="admin", kind="access")
    payload = decode_token(token)
    assert payload["sub"] == "1"
    assert payload["tid"] == "t-1"
    assert payload["role"] == "admin"
    assert payload["typ"] == "access"


def test_expired_token_rejected():
    from datetime import datetime, timezone

    from app.core.security import encode_payload
    from app.schemas.auth import TokenPayload

    token = encode_payload(
        TokenPayload(sub="1", tid="t-1", role="viewer", typ="access",
                     iat=datetime(2020, 1, 1, tzinfo=timezone.utc),
                     exp=datetime(2020, 1, 1, 0, 1, tzinfo=timezone.utc))
    )
    with pytest.raises(Exception):
        decode_token(token)
