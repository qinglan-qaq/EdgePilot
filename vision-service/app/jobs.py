from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .inference import InferenceError, JobCancelled, JobSpec
from .model import sha256_file
from .standardize import SCHEMA_VERSION, artifact_digest

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    """Persists job state to disk so restarts are recoverable and honest."""

    def __init__(self, settings: Settings, engine: Any) -> None:
        self.settings = settings
        self.engine = engine
        self.jobs_dir = Path(settings.jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._cancel: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings.max_workers), thread_name_prefix="vision-job"
        )
        self._shutdown = False
        self._recover()

    # ------------------------------------------------------------------ storage
    def _job_dir(self, task_uid: str) -> Path:
        return self.jobs_dir / task_uid

    def _state_path(self, task_uid: str) -> Path:
        return self._job_dir(task_uid) / "state.json"

    def _result_path(self, task_uid: str) -> Path:
        return self._job_dir(task_uid) / "result.json"

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _read_state_file(self, task_uid: str) -> dict[str, Any] | None:
        path = self._state_path(task_uid)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("task_uid", task_uid)
        data.setdefault("progress", 0)
        data.setdefault("attempt", 1)
        data.setdefault("result_available", False)
        return data

    def _persist(self, state: dict[str, Any]) -> None:
        self._write_json_atomic(self._state_path(state["task_uid"]), state)

    def _recover(self) -> None:
        for entry in sorted(self.jobs_dir.iterdir()):
            if not entry.is_dir():
                continue
            state = self._read_state_file(entry.name)
            if state is None:
                continue
            if state.get("status") in {"pending", "running"}:
                state.update(
                    status="failed",
                    error_code="service_restarted",
                    error_message="推理服务重启导致任务中断，请重试",
                    finished_at=_now(),
                    result_available=False,
                )
                self._persist(state)
            self._states[entry.name] = state

    # ------------------------------------------------------------------ public
    def submit(self, spec: JobSpec) -> dict[str, Any]:
        if self._shutdown:
            raise InferenceError("service_shutting_down", "推理服务正在关闭")
        with self._lock:
            existing = self._states.get(spec.task_uid)
            if existing is not None and existing.get("status") in {
                "pending",
                "running",
                "completed",
            }:
                return dict(existing)
            self._cancel.discard(spec.task_uid)

            pending = sum(1 for s in self._states.values() if s.get("status") == "pending")
            if pending >= max(1, self.settings.max_pending):
                raise InferenceError("queue_full", f"排队任务已达上限 {self.settings.max_pending}")

            attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
            self._result_path(spec.task_uid).unlink(missing_ok=True)
            state = {
                "task_uid": spec.task_uid,
                "status": "pending",
                "progress": 0,
                "attempt": attempt,
                "created_at": spec.created_at.astimezone(timezone.utc).isoformat(),
                "started_at": None,
                "finished_at": None,
                "error_code": None,
                "error_message": None,
                "result_available": False,
                "result_bytes": None,
                "result_sha256": None,
                "result_file_sha256": None,
                "result_schema_version": None,
                "processed_frames": None,
                "detection_count": 0,
                "video": None,
                "model": None,
                "summary": None,
                "params": dict(spec.params or {}),
                "filename": spec.filename,
            }
            self._states[spec.task_uid] = state
            self._persist(state)
            self._executor.submit(self._run, spec)

        return dict(state)

    def get(self, task_uid: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(task_uid)
            if state is None:
                state = self._read_state_file(task_uid)
                if state is None:
                    raise KeyError(task_uid)
                self._states[task_uid] = state
            return dict(state)

    def cancel(self, task_uid: str) -> dict[str, Any]:
        with self._lock:
            state = self._states.get(task_uid) or self._read_state_file(task_uid)
            if state is None:
                raise KeyError(task_uid)
            if state.get("status") in TERMINAL_STATUSES:
                return dict(state)
            self._cancel.add(task_uid)
            if state.get("status") == "pending":
                state.update(
                    status="cancelled",
                    error_code="cancelled_by_user",
                    error_message="任务已取消",
                    finished_at=_now(),
                )
                self._persist(state)
            self._states[task_uid] = state
            return dict(state)

    def result(self, task_uid: str) -> dict[str, Any]:
        path = self._result_path(task_uid)
        if not path.is_file():
            raise FileNotFoundError(task_uid)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def queue_stats(self) -> dict[str, int]:
        with self._lock:
            states = list(self._states.values())
        return {
            "pending": sum(1 for s in states if s.get("status") == "pending"),
            "running": sum(1 for s in states if s.get("status") == "running"),
            "completed": sum(1 for s in states if s.get("status") == "completed"),
            "failed": sum(1 for s in states if s.get("status") == "failed"),
            "max_workers": int(self.settings.max_workers),
            "max_pending": int(self.settings.max_pending),
        }

    def shutdown(self, wait: bool = True) -> None:
        self._shutdown = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def is_cancelled(self, task_uid: str) -> bool:
        with self._lock:
            return task_uid in self._cancel

    # ------------------------------------------------------------------ worker
    def _update(self, task_uid: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            state = self._states[task_uid]
            state.update(fields)
            self._persist(state)
            return dict(state)

    def _run(self, spec: JobSpec) -> None:
        task_uid = spec.task_uid
        self._update(task_uid, status="running", started_at=_now(), progress=0)
        last_pct = -1

        def on_progress(pct: int, meta: dict[str, Any]) -> None:
            nonlocal last_pct
            if pct == last_pct:
                return
            last_pct = pct
            self._update(
                task_uid,
                progress=max(0, min(99, int(pct))),
                processed_frames=meta.get("processed_frames"),
                detection_count=int(meta.get("detection_count") or 0),
                sampled_every_n_frames=meta.get("sampled_every_n_frames"),
            )

        started = time.monotonic()
        try:
            artifact = self.engine.run(
                spec, on_progress, lambda: self.is_cancelled(task_uid)
            )
            if self.is_cancelled(task_uid):
                raise JobCancelled()
        except JobCancelled:
            self._result_path(task_uid).unlink(missing_ok=True)
            self._update(
                task_uid,
                status="cancelled",
                error_code="cancelled_by_user",
                error_message="任务已取消",
                finished_at=_now(),
                result_available=False,
            )
            return
        except InferenceError as exc:
            self._update(
                task_uid,
                status="failed",
                error_code=exc.code,
                error_message=exc.message[:512],
                finished_at=_now(),
                result_available=False,
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._update(
                task_uid,
                status="failed",
                error_code="inference_failed",
                error_message=f"推理失败: {exc}"[:512],
                finished_at=_now(),
                result_available=False,
            )
            return

        result_path = self._result_path(task_uid)
        # Result first: the task may only become completed after the artifact is durable.
        self._write_json_atomic(result_path, artifact)
        file_sha = sha256_file(result_path)
        canonical = artifact_digest(artifact)
        summary = artifact.get("summary") or {}
        video = artifact.get("video") or {}
        model = artifact.get("model") or {}
        self._update(
            task_uid,
            status="completed",
            progress=100,
            finished_at=_now(),
            result_available=True,
            result_bytes=result_path.stat().st_size,
            result_sha256=canonical,
            result_file_sha256=file_sha,
            result_schema_version=artifact.get("schema_version") or SCHEMA_VERSION,
            processed_frames=summary.get("processed_frames"),
            detection_count=int(summary.get("detection_count") or 0),
            video=video,
            model=model,
            summary=summary,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
