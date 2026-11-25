from app.observability.capture import resolve_mode


def test_off_wins_over_tenant_override_and_degrade():
    assert resolve_mode("OFF", tenant_override="FULL", degraded=True) == "OFF"


def test_invalid_value_falls_back_to_redacted():
    assert resolve_mode("bogus") == "REDACTED"


def test_degrade_downgrades_content_modes_to_metadata():
    assert resolve_mode("FULL", degraded=True) == "METADATA"
    assert resolve_mode("REDACTED", degraded=True) == "METADATA"
    assert resolve_mode("METADATA", degraded=True) == "METADATA"


def test_tenant_override_applies_only_when_not_off():
    assert resolve_mode("METADATA", tenant_override="FULL") == "FULL"
    assert resolve_mode("METADATA", tenant_override="OFF") == "METADATA"
