import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.mark.asyncio
async def test_metrics_exposes_prometheus_text_with_samples():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        await c.get("/health")
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "# HELP http_request_total" in r.text
    assert 'http_request_total{' in r.text
    assert 'status="200"' in r.text
