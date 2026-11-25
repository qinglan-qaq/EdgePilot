import uuid

import pytest

from app.core.tenancy import (
    TenantNotSetError,
    current_tenant_id_or_none,
    get_tenant_id,
    reset_tenant_id,
    set_tenant_id,
)


def test_get_tenant_id_raises_when_unset():
    with pytest.raises(TenantNotSetError):
        get_tenant_id()


def test_set_and_reset_tenant():
    tid = uuid.uuid4()
    token = set_tenant_id(tid)
    try:
        assert get_tenant_id() == tid
        assert current_tenant_id_or_none() == tid
    finally:
        reset_tenant_id(token)
    assert current_tenant_id_or_none() is None
