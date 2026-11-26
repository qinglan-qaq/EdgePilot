"""Task 10 单元测试：vision-service HTTP 客户端协议与错误映射。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.core.config import Settings
from app.services.vision_client import (
    VisionClient,
    VisionJobNotFound,
    VisionServiceError,
)


def _settings(**overrides) -> Settings:
    values = {
        "vision_service_url": "http://vision.test",
        "vision_service_token": "secret",
        "vision_service_timeout_s": 5.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _client(handler, **overrides) -> VisionClient:
    return VisionClient(_settings(**overrides), transport=httpx.MockTransport(handler))


async def test_health_sends_token_and_json_content_type():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("x-vision-token")
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(200, json={"status": "ok", "service": "edgepilot-vision"})

    state = await _client(handler).health()

    assert seen == {
        "method": "GET",
        "path": "/health",
        "token": "secret",
        "content_type": "application/json",
    }
    assert state == {"status": "ok", "service": "edgepilot-vision"}


async def test_models_uses_models_endpoint():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"models": [{"key": "yolo11n", "version": "8.3.0"}]})

    state = await _client(handler).models()

    assert seen == {"method": "GET", "path": "/v1/models"}
    assert state["models"][0]["key"] == "yolo11n"


async def test_probe_sends_video_path_and_returns_model_version():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "duration_ms": 1000,
                "fps": 25.0,
                "frame_count": 25,
                "model_version": "yolo11n-8.3.0",
            },
        )

    state = await _client(handler).probe("/data/clip.mp4")

    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/probe"
    assert seen["body"] == {"video_path": "/data/clip.mp4"}
    assert state["model_version"] == "yolo11n-8.3.0"


async def test_create_job_payload_and_token():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("x-vision-token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"task_uid": "t1", "status": "pending"})

    state = await _client(handler).create_job(
        "t1", "/data/v.mp4", "v.mp4", "a" * 64, 0.25, 900, 1
    )

    assert state["status"] == "pending"
    assert seen["path"] == "/v1/jobs"
    assert seen["token"] == "secret"
    body = seen["body"]
    assert body["task_uid"] == "t1"
    assert body["video_path"] == "/data/v.mp4"
    assert body["filename"] == "v.mp4"
    assert body["video_sha256"] == "a" * 64
    assert body["params"]["conf_threshold"] == 0.25
    assert body["params"]["target_samples"] == 900
    assert body["params"]["frame_stride"] == 1


async def test_create_job_omits_frame_stride_when_none():
    seen = {}

    def handler(request):
        seen["params"] = json.loads(request.content)["params"]
        return httpx.Response(202, json={"task_uid": "t1", "status": "pending"})

    state = await _client(handler).create_job(
        "t1", "/data/v.mp4", "v.mp4", "a" * 64, 0.25, 900, None
    )

    assert "frame_stride" not in seen["params"]
    assert seen["params"]["conf_threshold"] == 0.25
    assert seen["params"]["target_samples"] == 900
    assert state["status"] == "pending"


async def test_token_header_is_omitted_when_not_configured():
    seen = {}

    def handler(request):
        seen["token"] = request.headers.get("x-vision-token")
        return httpx.Response(200, json={"status": "ok"})

    await _client(handler, vision_service_token="").health()

    assert seen["token"] is None


async def test_get_job_uses_task_path():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"task_uid": "t1", "status": "running", "progress": 42}
        )

    state = await _client(handler).get_job("t1")

    assert seen == {"method": "GET", "path": "/v1/jobs/t1"}
    assert state["progress"] == 42


async def test_cancel_job_posts_to_cancel_path():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"task_uid": "t1", "status": "cancelled"})

    state = await _client(handler).cancel_job("t1")

    assert seen == {"method": "POST", "path": "/v1/jobs/t1/cancel"}
    assert state["status"] == "cancelled"


async def test_404_maps_to_vision_job_not_found():
    def handler(request):
        return httpx.Response(404, json={"detail": "job missing", "code": "job_not_found"})

    with pytest.raises(VisionJobNotFound) as exc_info:
        await _client(handler).get_job("missing")

    assert isinstance(exc_info.value, VisionJobNotFound)
    assert isinstance(exc_info.value, VisionServiceError)
    assert exc_info.value.status == 404


@pytest.mark.parametrize("status", [400, 422, 500, 503])
async def test_http_errors_map_to_vision_service_error(status):
    def handler(request):
        return httpx.Response(
            status, json={"detail": "upstream rejected", "code": "upstream_error"}
        )

    with pytest.raises(VisionServiceError) as exc_info:
        await _client(handler).health()

    assert isinstance(exc_info.value, VisionServiceError)
    assert not isinstance(exc_info.value, VisionJobNotFound)
    assert exc_info.value.status == status
    assert "upstream rejected" in str(exc_info.value)


async def test_error_detail_dict_is_flattened_into_message():
    def handler(request):
        return httpx.Response(
            422,
            json={
                "detail": {
                    "code": "invalid_params",
                    "message": "frame_stride must be 1..300",
                }
            },
        )

    with pytest.raises(VisionServiceError) as exc_info:
        await _client(handler).create_job(
            "t1", "/data/v.mp4", "v.mp4", "a" * 64, 0.25, 900, 1
        )

    assert exc_info.value.status == 422
    assert "frame_stride must be 1..300" in str(exc_info.value)


async def test_non_json_error_body_still_raises_service_error():
    def handler(request):
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(VisionServiceError) as exc_info:
        await _client(handler).health()

    assert exc_info.value.status == 502


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_transport_errors_map_to_vision_service_error(error_type):
    def handler(request):
        raise error_type("boom", request=request)

    with pytest.raises(VisionServiceError) as exc_info:
        await _client(handler).health()

    assert isinstance(exc_info.value, VisionServiceError)
    assert not isinstance(exc_info.value, VisionJobNotFound)
    assert exc_info.value.status is None


_METHOD_CALLS = {
    "health": lambda client: client.health(),
    "models": lambda client: client.models(),
    "probe": lambda client: client.probe("/data/v.mp4"),
    "create_job": lambda client: client.create_job(
        "t1", "/data/v.mp4", "v.mp4", "a" * 64, 0.25, 900, 1
    ),
    "get_job": lambda client: client.get_job("t1"),
    "cancel_job": lambda client: client.cancel_job("t1"),
}


@pytest.mark.parametrize("method_name", sorted(_METHOD_CALLS))
async def test_every_call_uses_configured_timeout(method_name):
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={})

    client = _client(handler, vision_service_timeout_s=0.125)
    await _METHOD_CALLS[method_name](client)

    assert seen["timeout"]["read"] == 0.125
    assert seen["timeout"]["connect"] == 0.125


async def test_network_log_does_not_leak_full_url_or_token(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.vision_client")

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = _client(
        handler,
        vision_service_url="http://secret-vision-host:8001",
        vision_service_token="super-secret-token",
    )
    with pytest.raises(VisionServiceError) as exc_info:
        await client.health()

    app_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("app.services.vision_client")
    )
    assert "secret-vision-host" not in app_logs
    assert "8001" not in app_logs
    assert "super-secret-token" not in app_logs
    assert "secret-vision-host" not in str(exc_info.value)
    assert "super-secret-token" not in str(exc_info.value)
