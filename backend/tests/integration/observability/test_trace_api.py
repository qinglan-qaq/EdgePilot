import pytest

from tests.conftest import auth_headers


@pytest.mark.asyncio
async def test_list_and_tree(client, viewer_token, seeded_trace):
    r = await client.get("/api/v1/observability/traces", headers=auth_headers(viewer_token))
    assert r.json()["code"] == 0
    assert r.json()["data"]["total"] >= 1

    tree = await client.get(f"/api/v1/observability/traces/{seeded_trace}/tree",
                            headers=auth_headers(viewer_token))
    roots = tree.json()["data"]
    assert roots[0]["span_type"] == "workflow"
    assert any(c["span_type"] == "agent" for c in roots[0]["children"])


@pytest.mark.asyncio
async def test_cross_tenant_trace_is_404(client, other_tenant_token, seeded_trace):
    r = await client.get(f"/api/v1/observability/traces/{seeded_trace}",
                         headers=auth_headers(other_tenant_token))
    assert r.status_code == 404
