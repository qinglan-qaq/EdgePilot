"""vision-service HTTP 客户端。

只做协议转换与错误映射，不记录完整 URL / token。连接失败与 5xx 统一
``VisionServiceError``；404 单独成 ``VisionJobNotFound`` 供轮询器区分
"任务丢失" 与 "服务不可达"。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class VisionServiceError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class VisionJobNotFound(VisionServiceError):
    pass


class VisionClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.vision_service_token:
            headers["X-Vision-Token"] = self.settings.vision_service_token
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.settings.vision_service_url.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.vision_service_timeout_s,
                transport=self.transport,
            ) as client:
                response = await client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("vision-service 不可达 (path=%s, error=%s)", path, type(exc).__name__)
            raise VisionServiceError(f"推理服务不可达: {type(exc).__name__}") from exc

        if response.status_code == 404:
            raise VisionJobNotFound("推理服务任务不存在", status=404)
        if response.status_code >= 400:
            body: dict[str, Any] = {}
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                body = {}
            detail = body.get("detail") or body.get("message") or "推理服务返回错误"
            code = body.get("code")
            if isinstance(detail, dict):
                code = detail.get("code") or code
                detail = detail.get("message") or detail
            raise VisionServiceError(
                str(detail), status=response.status_code, code=code
            )
        try:
            return response.json()
        except ValueError as exc:
            raise VisionServiceError("推理服务返回非 JSON 响应") from exc

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def models(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/models")

    async def probe(self, video_path: str) -> dict[str, Any]:
        return await self._request("POST", "/v1/probe", json={"video_path": video_path})

    async def create_job(
        self,
        task_uid: str,
        video_path: str,
        filename: str,
        video_sha256: str,
        conf_threshold: float,
        target_samples: int,
        frame_stride: int | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "conf_threshold": float(conf_threshold),
            "target_samples": int(target_samples),
        }
        # frame_stride 不传时由 vision-service 使用默认步长；显式 null 会 422，必须省略该键。
        if frame_stride is not None:
            params["frame_stride"] = int(frame_stride)
        payload = {
            "task_uid": task_uid,
            "video_path": video_path,
            "filename": filename,
            "video_sha256": video_sha256,
            "params": params,
        }
        return await self._request("POST", "/v1/jobs", json=payload)

    async def get_job(self, task_uid: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/jobs/{task_uid}")

    async def cancel_job(self, task_uid: str) -> dict[str, Any]:
        return await self._request("POST", f"/v1/jobs/{task_uid}/cancel")

    async def results(self, task_uid: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/jobs/{task_uid}/results")
