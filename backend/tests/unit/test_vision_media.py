"""Task 15 单元测试：媒体签名/验签、文件哈希绑定、TTL、Range 解析与 206 流。

token 绑定 tenant + task + video file sha256（payload 的 ``f`` 字段）；任一不匹配
都必须 403。Range 语法错误/越界抛 ``RangeNotSatisfiable``（416），由路由补
``Content-Range: bytes */size``。
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.exceptions import AppError, Forbidden
from app.services.vision_media import (
    MediaToken,
    RangeNotSatisfiable,
    parse_range,
    sign_media_token,
    stream_video,
    verify_media_token,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000002")
TASK_UID = "task-media-1"
FILE_SHA = "a" * 64
OTHER_SHA = "b" * 64
NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)


def _decode_payload(token: str) -> dict:
    payload = token.split(".", 1)[0]
    padding = "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload + padding))


def _signed(*, ttl_s: int = 3600, now: datetime = NOW) -> MediaToken:
    return sign_media_token(TENANT, TASK_UID, FILE_SHA, ttl_s=ttl_s, now=now)


async def _body(response) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


# ---------------------------------------------------------------- 签名与验签


def test_sign_verify_roundtrip_includes_file_binding_payload():
    token = _signed()

    verify_media_token(token.token, token.expires_at, TENANT, TASK_UID, FILE_SHA, now=NOW)

    payload = _decode_payload(token.token)
    assert payload["t"] == str(TENANT)
    assert payload["u"] == TASK_UID
    assert payload["f"] == FILE_SHA
    assert payload["e"] == token.expires_at
    assert token.token.count(".") == 1


def test_default_ttl_is_one_hour():
    token = _signed()

    assert token.expires_at == int(NOW.timestamp()) + 3600


def test_verify_rejects_tenant_task_and_file_mismatch():
    token = _signed()

    with pytest.raises(Forbidden):
        verify_media_token(
            token.token, token.expires_at, OTHER_TENANT, TASK_UID, FILE_SHA, now=NOW
        )
    with pytest.raises(Forbidden):
        verify_media_token(
            token.token, token.expires_at, TENANT, "task-other", FILE_SHA, now=NOW
        )
    with pytest.raises(Forbidden):
        verify_media_token(
            token.token, token.expires_at, TENANT, TASK_UID, OTHER_SHA, now=NOW
        )


def test_verify_rejects_query_expires_mismatch():
    token = _signed()

    with pytest.raises(Forbidden):
        verify_media_token(
            token.token, token.expires_at + 1, TENANT, TASK_UID, FILE_SHA, now=NOW
        )


def test_expired_token_rejected_but_boundary_is_still_valid():
    expired = sign_media_token(
        TENANT, TASK_UID, FILE_SHA, ttl_s=1, now=NOW - timedelta(seconds=2)
    )
    with pytest.raises(Forbidden):
        verify_media_token(
            expired.token, expired.expires_at, TENANT, TASK_UID, FILE_SHA, now=NOW
        )

    token = _signed()
    boundary = datetime.fromtimestamp(token.expires_at, tz=timezone.utc)
    # e >= now 仍有效；超过 TTL 一秒即拒绝。
    verify_media_token(
        token.token, token.expires_at, TENANT, TASK_UID, FILE_SHA, now=boundary
    )
    with pytest.raises(Forbidden):
        verify_media_token(
            token.token,
            token.expires_at,
            TENANT,
            TASK_UID,
            FILE_SHA,
            now=boundary + timedelta(seconds=1),
        )


def test_tampered_and_malformed_tokens_rejected():
    token = _signed()
    payload, signature = token.token.split(".", 1)
    tampered_sig = ("B" if signature[0] != "B" else "C") + signature[1:]
    tampered_payload = payload[:5] + ("A" if payload[5] != "A" else "B") + payload[6:]
    other = sign_media_token(TENANT, TASK_UID, FILE_SHA, now=NOW + timedelta(seconds=1))

    for bad in (
        f"{payload}.{tampered_sig}",
        f"{tampered_payload}.{signature}",
        f"{other.token.split('.', 1)[0]}.{signature}",
        "not-a-token",
        "",
        "a.b",
    ):
        with pytest.raises(Forbidden):
            verify_media_token(bad, token.expires_at, TENANT, TASK_UID, FILE_SHA, now=NOW)


def test_verify_uses_constant_time_comparison(monkeypatch):
    calls: list[tuple] = []
    real_compare = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", spy)

    token = _signed()
    verify_media_token(token.token, token.expires_at, TENANT, TASK_UID, FILE_SHA, now=NOW)

    # 签名、expires、tenant/task/file 绑定必须走 hmac.compare_digest（常量时间）。
    assert calls


def test_verify_defaults_to_real_clock_for_expiry():
    """省略 now 时必须按真实当前时间判定：签发于 2 小时前的 1 小时 token 已过期。"""
    token = sign_media_token(
        TENANT,
        TASK_UID,
        FILE_SHA,
        ttl_s=3600,
        now=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    with pytest.raises(Forbidden):
        verify_media_token(token.token, token.expires_at, TENANT, TASK_UID, FILE_SHA)


# ---------------------------------------------------------------- Range 解析


def test_parse_range_valid_forms():
    assert parse_range("bytes=0-1023", 5000) == (0, 1023)
    assert parse_range("bytes=4000-", 5000) == (4000, 4999)
    assert parse_range("bytes=-500", 5000) == (4500, 4999)
    assert parse_range("bytes=0-0", 5000) == (0, 0)
    assert parse_range("bytes=0-4999", 5000) == (0, 4999)
    # 后缀超过文件大小取全量；end 超过文件末尾按最后字节截断。
    assert parse_range("bytes=-99999", 5000) == (0, 4999)
    assert parse_range("bytes=4900-99999", 5000) == (4900, 4999)


def test_parse_range_absent_header_means_full_content():
    assert parse_range(None, 5000) is None
    assert parse_range("", 5000) is None


@pytest.mark.parametrize(
    "header",
    [
        "bytes=6000-7000",    # start 越界
        "bytes=5000-",        # start == size
        "bytes=4000-3999",    # end < start
        "bytes=-0",           # 后缀必须 > 0
        "bytes=",             # 空范围
        "bytes=abc-def",      # 非数字
        "bytes=0-1,2-3",      # 多段不支持
        "items=0-1023",       # 单位错误
        "bytes=--5",          # 语法错误
    ],
)
def test_parse_range_invalid_raises_416(header: str):
    with pytest.raises(RangeNotSatisfiable) as exc_info:
        parse_range(header, 5000)

    error = exc_info.value
    assert isinstance(error, AppError)
    assert error.http_status == 416
    assert error.code == 416


def test_parse_range_empty_file_raises_416():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-0", 0)


# ---------------------------------------------------------------- 流式响应


def _write_video(tmp_path: Path, size: int = 5000) -> tuple[Path, bytes]:
    data = bytes(index % 251 for index in range(size))
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    return path, data


async def test_stream_video_without_range_returns_200_full_content(tmp_path):
    path, data = _write_video(tmp_path)

    response = stream_video(path, None)

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["content-type"].startswith("video/mp4")
    assert await _body(response) == data


async def test_stream_video_with_range_returns_206_and_content_range(tmp_path):
    path, data = _write_video(tmp_path)
    byte_range = parse_range("bytes=10-19", 5000)

    response = stream_video(path, byte_range)

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 10-19/5000"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-disposition"] == "inline"
    assert await _body(response) == data[10:20]


async def test_stream_video_suffix_range_returns_tail(tmp_path):
    path, data = _write_video(tmp_path)

    response = stream_video(path, parse_range("bytes=-100", 5000))

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 4900-4999/5000"
    assert await _body(response) == data[-100:]


def test_range_not_satisfiable_is_app_error_416():
    # 路由捕获该异常并自行返回 Response(416, Content-Range: bytes */size)；
    # 不能依赖全局 JSON handler（它不会带 Content-Range 头）。
    try:
        parse_range("bytes=999999-", 5000)
    except RangeNotSatisfiable as error:
        assert isinstance(error, AppError)
        assert error.http_status == 416
        assert error.code == 416
    else:  # pragma: no cover - 越界必须抛错
        raise AssertionError("越界 Range 必须抛 RangeNotSatisfiable")
