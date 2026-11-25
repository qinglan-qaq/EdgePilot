VALID_MODES = ("OFF", "METADATA", "REDACTED", "FULL")


def resolve_mode(configured: str, tenant_override: str | None = None,
                 *, degraded: bool = False) -> str:
    mode = configured if configured in VALID_MODES else "REDACTED"
    if mode == "OFF":
        return "OFF"
    if tenant_override in VALID_MODES and tenant_override != "OFF":
        mode = tenant_override
    if degraded and mode in ("FULL", "REDACTED"):
        return "METADATA"
    return mode
