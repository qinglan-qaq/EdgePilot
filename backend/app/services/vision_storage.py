"""Vision 文件存储：视频落盘、路径防护、artifact 读取与哈希校验。

视频与帧级结果都不入 PostgreSQL。所有路径都相对 ``vision_storage_root``，
读取前必须 ``resolve()`` 并确认仍在根目录内，阻止 ``..`` 与绝对路径。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.exceptions import AppError, InsufficientStorage, PayloadTooLarge

ALLOWED_SUFFIXES = {".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"}
RESULT_SCHEMA_VERSION = "vision-result/v1"


class InvalidFileType(AppError):
    code = http_status = 400


class ArtifactMissing(AppError):
    code = http_status = 404


class ArtifactCorrupted(AppError):
    code = http_status = 503


class ArtifactUnreadable(AppError):
    code = http_status = 422


@dataclass
class SavedUpload:
    sha256: str
    size: int
    ext: str
    tmp_path: Path
    final_path: Path
    relative_path: str


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_digest(artifact: dict[str, Any]) -> str:
    payload = json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class VisionStorage:
    def __init__(self, root: str | Path, *, max_upload_mb: int = 200) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = max_upload_mb * 1024 * 1024
        self.videos_root = self.root / "videos"
        self.jobs_root = self.root / "jobs"

    # ------------------------------------------------------------------ paths
    def _tenant_dir(self, tenant_id: uuid.UUID | str) -> Path:
        return self.videos_root / str(tenant_id)

    def temp_path(self, tenant_id: uuid.UUID | str, task_uid: str, ext: str) -> Path:
        return self._tenant_dir(tenant_id) / f".tmp-{task_uid}{ext}"

    def final_path(self, tenant_id: uuid.UUID | str, task_uid: str, ext: str) -> Path:
        return self._tenant_dir(tenant_id) / f"{task_uid}{ext}"

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError("路径不在 vision 存储根目录内") from exc

    def resolve(self, relative_path: str | Path) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute():
            raise ValueError("只接受相对存储根的路径")
        resolved = (self.root / raw).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径不在 vision 存储根目录内")
        return resolved

    def artifact_relative(self, task_uid: str) -> str:
        return f"jobs/{task_uid}/result.json"

    def artifact_path(self, task_uid: str) -> Path:
        return self.jobs_root / task_uid / "result.json"

    # ----------------------------------------------------------------- upload
    def validate_magic(self, head: bytes, ext: str) -> None:
        ext = ext.lower()
        if ext in {".mp4", ".mov", ".m4v"}:
            ok = len(head) >= 8 and head[4:8] == b"ftyp"
        elif ext in {".webm", ".mkv"}:
            ok = head[:4] == b"\x1a\x45\xdf\xa3"
        elif ext == ".avi":
            ok = len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"AVI "
        else:
            ok = False
        if not ok:
            raise InvalidFileType(
                "文件内容与扩展名不匹配或不是受支持的视频",
                detail={"reason": "invalid_file_type"},
            )

    async def save_upload(self, upload: Any, tenant_id: uuid.UUID, task_uid: str) -> SavedUpload:
        filename = getattr(upload, "filename", "") or ""
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_SUFFIXES:
            raise InvalidFileType(
                f"不支持的视频类型: {ext or '未知'}",
                detail={"reason": "invalid_file_type"},
            )

        tmp = self.temp_path(tenant_id, task_uid, ext)
        hasher = hashlib.sha256()
        size = 0
        head = b""
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise PayloadTooLarge(
                            f"视频超过 {self.max_bytes // (1024 * 1024)}MB 上限",
                            detail={"reason": "file_too_large"},
                        )
                    if len(head) < 32:
                        head += chunk[: 32 - len(head)]
                    hasher.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size <= 0:
                raise InvalidFileType(
                    "视频文件为空", detail={"reason": "invalid_file_type"}
                )
            self.validate_magic(head, ext)
            final = self.final_path(tenant_id, task_uid, ext)
            return SavedUpload(
                sha256=hasher.hexdigest(),
                size=size,
                ext=ext,
                tmp_path=tmp,
                final_path=final,
                relative_path=self.relative(final),
            )
        except AppError:
            tmp.unlink(missing_ok=True)
            raise
        except OSError as exc:
            # 磁盘满/权限/IO 错误统一转 507，避免泄露底层 errno 文案给前端。
            tmp.unlink(missing_ok=True)
            raise InsufficientStorage(
                "视频写入失败，请稍后重试或联系管理员检查存储",
                detail={"reason": "storage_write_failed"},
            ) from exc
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def move_into_place(self, saved: SavedUpload) -> None:
        try:
            saved.final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(saved.tmp_path, saved.final_path)
        except OSError as exc:
            raise InsufficientStorage(
                "视频写入失败，请稍后重试或联系管理员检查存储",
                detail={"reason": "storage_write_failed"},
            ) from exc

    def remove(self, relative_path: str | Path) -> None:
        try:
            self.resolve(relative_path).unlink(missing_ok=True)
        except ValueError:
            return

    def remove_job_dir(self, task_uid: str) -> int:
        job_dir = (self.jobs_root / task_uid).resolve()
        if not job_dir.is_dir() or not job_dir.is_relative_to(self.root):
            return 0
        deleted = 0
        for child in sorted(job_dir.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink(missing_ok=True)
                deleted += 1
            elif child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    pass
        try:
            job_dir.rmdir()
        except OSError:
            pass
        return deleted

    # ---------------------------------------------------------------- artifact
    def read_artifact(
        self,
        task_uid: str,
        expected_file_sha256: str | None,
        expected_bytes: int | None,
        expected_schema: str | None,
    ) -> dict[str, Any]:
        path = self.artifact_path(task_uid)
        if not path.is_file():
            raise ArtifactMissing("结果文件不存在或已被清理", detail={"reason": "artifact_missing"})
        size = path.stat().st_size
        if expected_bytes is not None and size != int(expected_bytes):
            raise ArtifactCorrupted(
                "结果文件大小与任务记录不一致",
                detail={"reason": "artifact_corrupted", "expected_bytes": expected_bytes, "actual_bytes": size},
            )
        file_sha = sha256_file(path)
        if expected_file_sha256 and file_sha != expected_file_sha256:
            raise ArtifactCorrupted(
                "结果文件哈希与任务记录不一致",
                detail={"reason": "artifact_corrupted"},
            )
        try:
            with path.open("r", encoding="utf-8") as handle:
                artifact = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactUnreadable(
                "结果文件无法解析", detail={"reason": "artifact_unreadable"}
            ) from exc
        if not isinstance(artifact, dict):
            raise ArtifactUnreadable("结果文件结构非法", detail={"reason": "artifact_unreadable"})
        schema = artifact.get("schema_version")
        if expected_schema and schema != expected_schema:
            raise ArtifactUnreadable(
                f"结果 schema 不符: {schema}", detail={"reason": "artifact_unreadable"}
            )
        if schema != RESULT_SCHEMA_VERSION:
            raise ArtifactUnreadable(
                f"不支持的结果 schema: {schema}", detail={"reason": "artifact_unreadable"}
            )
        return artifact

    def store_artifact(self, task_uid: str, artifact: dict[str, Any]) -> SavedUpload:
        """Best-effort helper for tests/demo seeders; the real writer is vision-service."""
        path = self.artifact_path(task_uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(artifact, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return SavedUpload(
            sha256=sha256_file(path),
            size=path.stat().st_size,
            ext=".json",
            tmp_path=tmp,
            final_path=path,
            relative_path=self.artifact_relative(task_uid),
        )
