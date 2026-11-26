"""Task 9 单元测试：上传落盘、路径防护与 artifact 读取校验。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import pytest

from app.core.exceptions import InsufficientStorage, PayloadTooLarge
from app.services.vision_storage import (
    ArtifactCorrupted,
    ArtifactMissing,
    ArtifactUnreadable,
    InvalidFileType,
    VisionStorage,
    artifact_digest,
)

TENANT = uuid.UUID("00000000-0000-0000-0000-000000000001")
RESULT_SCHEMA = "vision-result/v1"

AVI_BYTES = b"RIFF" + b"\x00" * 4 + b"AVI " + b"x" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20

ARTIFACT = {
    "schema_version": RESULT_SCHEMA,
    "task_uid": "task-1",
    "video": {"filename": "gate.mp4", "frame_count": 10},
    "detections": [{"frame_id": 1, "class_id": 0, "confidence": 0.9}],
    "events": [],
    "summary": {"detection_count": 1},
}


class SimpleUpload:
    """最小 UploadFile 替身：只实现 filename 与异步 read。"""

    def __init__(self, data: bytes, filename: str) -> None:
        self.data = data
        self.filename = filename
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self.data) - self._offset
        chunk = self.data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _write_artifact(root, task_uid: str, artifact: dict, *, indent: int | None = None):
    path = root / "jobs" / task_uid / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=indent), encoding="utf-8")
    raw = path.read_bytes()
    return path, hashlib.sha256(raw).hexdigest(), len(raw)


def _temp_files(root):
    return list((root / "videos").rglob(".tmp-*")) if (root / "videos").exists() else []


# ---------------------------------------------------------------- upload


async def test_save_upload_streams_and_hashes(tmp_path):
    storage = VisionStorage(str(tmp_path))
    upload = SimpleUpload(AVI_BYTES, "clip.avi")

    saved = await storage.save_upload(upload, TENANT, "task-1")

    assert saved.size == len(AVI_BYTES)
    assert saved.sha256 == hashlib.sha256(AVI_BYTES).hexdigest()
    assert saved.ext == ".avi"
    assert saved.tmp_path.is_file()
    assert saved.tmp_path.read_bytes() == AVI_BYTES
    assert saved.final_path.is_relative_to(tmp_path)
    storage.validate_magic(AVI_BYTES[:32], ".avi")


async def test_move_into_place_replaces_target_and_removes_temp(tmp_path):
    storage = VisionStorage(str(tmp_path))
    saved = await storage.save_upload(SimpleUpload(AVI_BYTES, "clip.avi"), TENANT, "task-1")
    saved.final_path.parent.mkdir(parents=True, exist_ok=True)
    saved.final_path.write_bytes(b"stale")

    storage.move_into_place(saved)

    assert not saved.tmp_path.exists()
    assert saved.final_path.is_file()
    assert saved.final_path.read_bytes() == AVI_BYTES


async def test_upload_over_limit_raises_413_and_removes_temp(tmp_path):
    storage = VisionStorage(str(tmp_path))
    storage.max_bytes = 16

    with pytest.raises(PayloadTooLarge) as exc_info:
        await storage.save_upload(SimpleUpload(AVI_BYTES, "clip.avi"), TENANT, "task-big")

    assert exc_info.value.http_status == 413
    assert (exc_info.value.detail or {}).get("reason") == "file_too_large"
    assert _temp_files(tmp_path) == []


async def test_unknown_extension_is_rejected(tmp_path):
    storage = VisionStorage(str(tmp_path))

    with pytest.raises(InvalidFileType) as exc_info:
        await storage.save_upload(SimpleUpload(AVI_BYTES, "clip.txt"), TENANT, "task-txt")

    assert exc_info.value.http_status == 400
    assert _temp_files(tmp_path) == []


async def test_wrong_magic_on_save_raises_and_removes_temp(tmp_path):
    storage = VisionStorage(str(tmp_path))
    upload = SimpleUpload(b"not-a-video" + b"\x00" * 20, "clip.mp4")

    with pytest.raises(InvalidFileType):
        await storage.save_upload(upload, TENANT, "task-magic")

    assert _temp_files(tmp_path) == []


async def test_oserror_maps_to_507_and_removes_temp(tmp_path, monkeypatch):
    storage = VisionStorage(str(tmp_path))

    def disk_full(fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)

    with pytest.raises(InsufficientStorage) as exc_info:
        await storage.save_upload(SimpleUpload(AVI_BYTES, "clip.avi"), TENANT, "task-full")

    assert exc_info.value.http_status == 507
    assert (exc_info.value.detail or {}).get("reason") == "storage_write_failed"
    assert _temp_files(tmp_path) == []


# ---------------------------------------------------------------- magic / paths


def test_validate_magic_accepts_supported_containers_and_rejects_mismatch(tmp_path):
    storage = VisionStorage(str(tmp_path))

    storage.validate_magic(AVI_BYTES[:32], ".avi")
    storage.validate_magic(MP4_BYTES[:32], ".mp4")

    with pytest.raises(InvalidFileType) as exc_info:
        storage.validate_magic(b"not-a-video" + b"\x00" * 20, ".mp4")
    assert exc_info.value.http_status == 400


def test_path_traversal_and_absolute_paths_are_rejected(tmp_path):
    storage = VisionStorage(str(tmp_path))

    with pytest.raises(ValueError):
        storage.resolve("../../etc/passwd")
    with pytest.raises(ValueError):
        storage.resolve(str(tmp_path.parent / "outside.txt"))

    inside = storage.resolve(f"videos/{TENANT}/clip.avi")
    assert inside.is_relative_to(tmp_path)


# ---------------------------------------------------------------- artifacts


def test_artifact_digest_is_canonical_json_sha256():
    artifact = {"b": 2, "a": {"y": [1, 2], "x": "中文"}}
    expected = hashlib.sha256(
        json.dumps(
            artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()

    assert artifact_digest(artifact) == expected
    assert artifact_digest({"a": 1, "b": 2}) == artifact_digest({"b": 2, "a": 1})


def test_read_artifact_verifies_file_sha_bytes_and_schema(tmp_path):
    storage = VisionStorage(str(tmp_path))
    _, file_sha, size = _write_artifact(tmp_path, "task-1", ARTIFACT, indent=2)

    # 文件字节哈希与 canonical JSON 哈希是两个字段，不能混用。
    assert file_sha != artifact_digest(ARTIFACT)

    loaded = storage.read_artifact("task-1", file_sha, size, RESULT_SCHEMA)
    assert loaded == ARTIFACT

    with pytest.raises(ArtifactCorrupted):
        storage.read_artifact("task-1", artifact_digest(ARTIFACT), size, RESULT_SCHEMA)


def test_artifact_file_hash_mismatch_raises(tmp_path):
    storage = VisionStorage(str(tmp_path))
    _, _, size = _write_artifact(tmp_path, "task-1", ARTIFACT)

    with pytest.raises(ArtifactCorrupted) as exc_info:
        storage.read_artifact("task-1", "0" * 64, size, RESULT_SCHEMA)

    assert exc_info.value.http_status == 503


def test_artifact_size_mismatch_raises(tmp_path):
    storage = VisionStorage(str(tmp_path))
    _, file_sha, size = _write_artifact(tmp_path, "task-1", ARTIFACT)

    with pytest.raises(ArtifactCorrupted):
        storage.read_artifact("task-1", file_sha, size + 1, RESULT_SCHEMA)


def test_artifact_missing_raises(tmp_path):
    storage = VisionStorage(str(tmp_path))

    with pytest.raises(ArtifactMissing) as exc_info:
        storage.read_artifact("ghost", "0" * 64, 1, RESULT_SCHEMA)

    assert exc_info.value.http_status == 404


def test_artifact_schema_mismatch_raises_unreadable(tmp_path):
    storage = VisionStorage(str(tmp_path))
    _, file_sha, size = _write_artifact(tmp_path, "task-1", ARTIFACT)

    with pytest.raises(ArtifactUnreadable) as exc_info:
        storage.read_artifact("task-1", file_sha, size, "vision-result/v2")

    assert exc_info.value.http_status == 422


def test_artifact_invalid_json_raises_unreadable(tmp_path):
    storage = VisionStorage(str(tmp_path))
    path = tmp_path / "jobs" / "task-1" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")
    raw = path.read_bytes()

    with pytest.raises(ArtifactUnreadable) as exc_info:
        storage.read_artifact(
            "task-1", hashlib.sha256(raw).hexdigest(), len(raw), RESULT_SCHEMA
        )

    assert exc_info.value.http_status == 422
