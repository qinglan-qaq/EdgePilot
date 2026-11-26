"""视频访问签名与 Range 流。

签名绑定 tenant_id + task_uid + 视频文件 SHA-256（任一不匹配即 403），TTL 默认
1 小时；校验使用 ``hmac.compare_digest``。Range 解析失败抛
``RangeNotSatisfiable``，由路由补 ``Content-Range: bytes */size``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi.responses import Response, StreamingResponse

from app.core.config import get_settings
from app.core.exceptions import AppError, Forbidden


class RangeNotSatisfiable(AppError):
    code = http_status = 416


@dataclass(frozen=True)
class MediaToken:
    token: str
    expires_at: int


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _key(secret: str | None = None) -> bytes:
    secret = secret or get_settings().jwt_secret
    return hmac.new(secret.encode("utf-8"), b"vision-media", hashlib.sha256).digest()


def sign_media_token(
    tenant_id: uuid.UUID | str,
    task_uid: str,
    file_id: str,
    *,
    ttl_s: int = 3600,
    now: datetime | None = None,
    secret: str | None = None,
) -> MediaToken:
    """签发绑定 租户 + 任务 + 文件指纹（SHA-256）的媒体 token，默认 1 小时有效。"""
    current = now or datetime.now(timezone.utc)
    issued = int(current.timestamp())
    expires = issued + int(ttl_s)
    payload = _b64encode(
        json.dumps(
            {"t": str(tenant_id), "u": task_uid, "f": file_id, "e": expires},
            separators=(",", ":"),
        ).encode("utf-8")
    )
    signature = _b64encode(hmac.new(_key(secret), payload.encode("ascii"), hashlib.sha256).digest())
    return MediaToken(token=f"{payload}.{signature}", expires_at=expires)


def _same(left: object, right: object) -> bool:
    return hmac.compare_digest(str(left), str(right))


def verify_media_token(
    token: str,
    expires: int | str,
    tenant_id: uuid.UUID | str,
    task_uid: str,
    file_id: str,
    *,
    now: datetime | None = None,
    secret: str | None = None,
) -> None:
    """校验签名、query expires、租户/任务/文件绑定与有效期；失败统一 403。"""
    try:
        payload_text, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(_key(secret), payload_text.encode("ascii"), hashlib.sha256).digest())
        if not _same(signature, expected):
            raise ValueError("signature")
        payload = json.loads(_b64decode(payload_text))
        if not isinstance(payload, dict):
            raise ValueError("payload")
        if not _same(payload.get("e"), expires):
            raise ValueError("expires")
        if not _same(payload.get("t"), str(tenant_id)):
            raise ValueError("tenant")
        if not _same(payload.get("u"), task_uid):
            raise ValueError("task")
        if not _same(payload.get("f"), file_id):
            raise ValueError("file")
        # 省略 now 时按真实当前时间判定，签名链接最长只有 TTL（默认 1 小时）。
        current = int((now or datetime.now(timezone.utc)).timestamp())
        if int(payload.get("e") or 0) < current:
            raise ValueError("expired")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise Forbidden("媒体链接无效或已过期", detail={"reason": "invalid_media_token"}) from None


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header:
        return None
    if size <= 0:
        raise RangeNotSatisfiable("视频文件为空")
    value = header.strip()
    if not value.startswith("bytes=") or "," in value:
        raise RangeNotSatisfiable("不支持的 Range 语法")
    spec = value[len("bytes="):].strip()
    try:
        start_text, end_text = spec.split("-", 1)
        if start_text == "":
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError
            start = max(0, size - suffix)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError:
        raise RangeNotSatisfiable("Range 语法错误") from None
    if start < 0 or start >= size or end < start:
        raise RangeNotSatisfiable("Range 越界")
    end = min(end, size - 1)
    return start, end


def _iter_file(path: Path, start: int, length: int, chunk_size: int = 256 * 1024):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def stream_video(path: Path, byte_range: tuple[int, int] | None) -> Response:
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
        "Content-Disposition": "inline",
    }
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(
            _iter_file(path, 0, size), media_type=media_type, headers=headers
        )
    start, end = byte_range
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _iter_file(path, start, length),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )
