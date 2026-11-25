import uuid

import pytest

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.user import User
from app.repositories.user_repo import UserRepo


@pytest.mark.asyncio
async def test_repository_filters_by_tenant(session):
    tid_a, tid_b = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        User(tenant_id=tid_a, email="a@x.com", password_hash="h", display_name="A",
             role="admin", status="active"),
        User(tenant_id=tid_b, email="b@x.com", password_hash="h", display_name="B",
             role="admin", status="active"),
    ])
    await session.commit()

    token = set_tenant_id(tid_a)
    try:
        repo = UserRepo(session)
        assert await repo.count() == 1
        assert (await repo.get_by_email("b@x.com")) is None
    finally:
        reset_tenant_id(token)
