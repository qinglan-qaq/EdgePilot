import pytest
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import NotFound
from app.main import create_app


@pytest.fixture
def app():
    app = create_app()

    @app.get("/_boom")
    async def boom():
        raise NotFound("工作流不存在")

    @app.get("/_ok")
    async def ok_endpoint():
        from app.schemas.common import ok

        return ok({"a": 1})

    return app


@pytest.mark.asyncio
async def test_success_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/_ok")
    body = r.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["data"] == {"a": 1}
    assert body["trace_id"] == r.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_error_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/_boom")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == 404
    assert body["message"] == "工作流不存在"
    assert body["data"] is None
