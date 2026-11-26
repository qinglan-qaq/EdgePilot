from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from .config import COMMERCIAL_NOTICE, LICENSE_NAME, LICENSE_URL, Settings


class ModelUnavailable(RuntimeError):
    """Raised when inference is requested but the weights/model are not usable."""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelRegistry:
    """Owns the Ultralytics model and reports honest load state.

    The registry never reports ``ready`` without an actually loaded model, and it
    never fabricates detections: when weights are missing, callers get an error.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any | None = None
        self._error: str | None = None
        self._sha256: str | None = None
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return Path(self.settings.model_path)

    @property
    def weights_sha256(self) -> str | None:
        if self._sha256 is None and self.model_path.is_file():
            self._sha256 = sha256_file(self.model_path)
        return self._sha256

    @property
    def model_name(self) -> str:
        return self.settings.model_key

    @property
    def model_version(self) -> str | None:
        sha = self.weights_sha256
        return f"{self.model_name}-{sha[:12]}" if sha else None

    @property
    def load_status(self) -> str:
        if self._model is not None:
            return "ready"
        if self._error is not None:
            return "load_error"
        if not self.model_path.is_file():
            return "missing_weights"
        return "not_loaded"

    def info(self) -> dict[str, Any]:
        status = self.load_status
        classes: list[str] = []
        if self._model is not None:
            names = getattr(self._model, "names", None) or {}
            classes = [str(names[i]) for i in sorted(names)] if isinstance(names, dict) else list(names)
        return {
            "status": status,
            "load_status": status,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "weights_path": self.model_path.name,
            "weights_sha256": self.weights_sha256,
            "device": self.settings.device,
            "imgsz": self.settings.imgsz,
            "class_count": len(classes),
            "classes": classes,
            "license": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "commercial_use_notice": COMMERCIAL_NOTICE,
            "error": self._error,
        }

    def load(self) -> Any:
        with self._lock:
            if self._model is not None:
                return self._model
            if not self.model_path.is_file():
                self._error = f"weights not found: {self.model_path.name}"
                raise ModelUnavailable(self._error)
            try:
                from ultralytics import YOLO  # imported lazily: torch is heavy

                model = YOLO(str(self.model_path))
                if self.settings.device and self.settings.device != "auto":
                    try:
                        model.to(self.settings.device)
                    except Exception:  # pragma: no cover - device specifics
                        # Keep CPU fallback but record it; never silently claim GPU.
                        self.settings = type(self.settings)(
                            **{**self.settings.__dict__, "device": "cpu"}
                        )
                self._model = model
                self._error = None
                return model
            except ModelUnavailable:
                raise
            except Exception as exc:  # pragma: no cover - depends on local torch
                self._error = f"model load failed: {exc}"
                raise ModelUnavailable(self._error) from exc

    def reload(self) -> dict[str, Any]:
        with self._lock:
            self._model = None
            self._error = None
            self._sha256 = None
        if self.settings.preload:
            try:
                self.load()
            except ModelUnavailable:
                pass
        return self.info()

    def predict(
        self,
        frame: Any,
        *,
        conf: float,
        imgsz: int,
        iou: float,
        max_det: int,
        device: str,
    ) -> list[dict[str, Any]]:
        model = self.load()
        results = model.predict(
            frame,
            conf=conf,
            imgsz=imgsz,
            iou=iou,
            max_det=max_det,
            device=device,
            verbose=False,
        )
        detections: list[dict[str, Any]] = []
        for result in results:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                continue
            names = getattr(result, "names", None) or getattr(model, "names", {}) or {}
            for box in boxes:
                xyxy = box.xyxy[0].tolist()
                class_id = int(box.cls[0].item())
                confidence = float(box.conf[0].item())
                class_name = str(names.get(class_id, class_id)) if isinstance(names, dict) else str(names[class_id])
                detections.append(
                    {
                        "class_id": class_id,
                        "class_name": class_name,
                        "confidence": confidence,
                        "bbox": {
                            "x1": float(xyxy[0]),
                            "y1": float(xyxy[1]),
                            "x2": float(xyxy[2]),
                            "y2": float(xyxy[3]),
                        },
                    }
                )
        return detections
