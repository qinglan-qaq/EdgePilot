import pytest

from tests.conftest import auth_headers


@pytest.mark.asyncio
async def test_publish_does_not_mutate_existing_version(client, admin_token):
    tpl = (await client.post("/api/v1/observability/prompts", headers=auth_headers(admin_token),
                             json={"template_id": "vision_analysis", "template_name": "视觉分析",
                                   "category": "agent"})).json()["data"]
    v1 = await client.post(f"/api/v1/observability/prompts/{tpl['id']}/versions",
                           headers=auth_headers(admin_token),
                           json={"system_prompt": "S1", "user_prompt_template": "U {{q}}",
                                 "variables_schema": {"type": "object", "required": ["q"],
                                                      "properties": {"q": {"type": "string"}}}})
    assert v1.json()["data"]["version"] == 1

    await client.post(f"/api/v1/observability/prompts/{tpl['id']}/versions",
                      headers=auth_headers(admin_token),
                      json={"system_prompt": "S2", "user_prompt_template": "U2 {{q}}",
                            "variables_schema": {"type": "object"}})
    await client.post(f"/api/v1/observability/prompts/{tpl['id']}/publish",
                     headers=auth_headers(admin_token), json={"version": 2})
    await client.post(f"/api/v1/observability/prompts/{tpl['id']}/rollback",
                     headers=auth_headers(admin_token), json={"version": 1})

    detail = (await client.get(f"/api/v1/observability/prompts/{tpl['id']}",
                               headers=auth_headers(admin_token))).json()["data"]
    assert detail["active_version"] == 1
    assert [v["version"] for v in detail["versions"]] == [2, 1]
    assert detail["versions"][1]["system_prompt"] == "S1"


@pytest.mark.asyncio
async def test_viewer_cannot_publish_prompt(client, viewer_token):
    r = await client.post("/api/v1/observability/prompts/1/publish",
                          headers=auth_headers(viewer_token), json={"version": 1})
    assert r.status_code == 403
