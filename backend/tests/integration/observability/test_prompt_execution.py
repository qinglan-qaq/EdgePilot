import pytest
from sqlalchemy import text

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability.prompt.recorder import record_execution
from tests.conftest import TENANT


@pytest.mark.asyncio
async def test_record_execution_persists_rendered_messages(session):
    messages = [{"role": "system", "content": "S"},
                {"role": "user", "content": "U hi"}]
    token = set_tenant_id(TENANT)
    try:
        await record_execution(session, trace_id="f" * 32, span_id="d" * 16,
                               template_id=1, version=3, variables={"q": "hi"},
                               messages=messages, status="ok")
    finally:
        reset_tenant_id(token)

    row = (await session.execute(text(
        "SELECT tenant_id, template_version, render_status, prompt_hash,"
        " rendered_messages->1->>'content' AS user_msg"
        " FROM prompt_executions WHERE trace_id=:t"), {"t": "f" * 32})).one()
    assert row.tenant_id == TENANT
    assert row.template_version == 3
    assert row.render_status == "ok"
    assert len(row.prompt_hash) == 64
    assert row.user_msg == "U hi"
