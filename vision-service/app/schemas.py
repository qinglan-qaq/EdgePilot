from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobParams(BaseModel):
    conf_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    frame_stride: int | None = Field(default=None, ge=1, le=300)
    target_samples: int | None = Field(default=None, ge=1, le=2000)
    imgsz: int | None = Field(default=None, ge=32, le=4096)
    iou: float | None = Field(default=None, gt=0.0, le=1.0)
    max_det: int | None = Field(default=None, ge=1, le=1000)


class JobCreate(BaseModel):
    task_uid: str = Field(min_length=1, max_length=36, pattern=r"^[A-Za-z0-9._-]+$")
    video_path: str = Field(min_length=1, max_length=1024)
    filename: str | None = Field(default=None, max_length=255)
    device_id: str | None = Field(default=None, max_length=64)
    video_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    params: JobParams = Field(default_factory=JobParams)

    @field_validator("filename")
    @classmethod
    def _strip_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.replace("\\", "/").split("/")[-1].strip()
        return cleaned[:255] or None


class ProbeIn(BaseModel):
    video_path: str = Field(min_length=1, max_length=1024)
    filename: str | None = Field(default=None, max_length=255)


class JobStateOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_uid: str
    status: str
    progress: int = 0
    attempt: int = 1
    result_available: bool = False
    error_code: str | None = None
    error_message: str | None = None
    progress_meta: dict[str, Any] | None = None
