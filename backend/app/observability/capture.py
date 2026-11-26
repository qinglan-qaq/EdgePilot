VALID_MODES = ("OFF", "METADATA", "REDACTED", "FULL")


def resolve_mode(configured: str, tenant_override: str | None = None,
                 *, degraded: bool = False) -> str:
    """把全局配置与租户覆盖合并成最终采集模式。

    关键节点：
        1. 非法配置回退到 REDACTED，生产默认不会误开到 FULL；
        2. 全局 OFF 是硬开关，租户覆盖不能把采集重新打开；
        3. 采集器降级时把 FULL/REDACTED 压到 METADATA，只保留元数据。

    Args:
        configured: 全局配置的采集模式。
        tenant_override: 租户级覆盖值；非法或 OFF 时忽略。
        degraded: 采集器是否处于降级状态。

    Returns:
        最终生效的模式：OFF / METADATA / REDACTED / FULL。
    """
    mode = configured if configured in VALID_MODES else "REDACTED"
    if mode == "OFF":
        return "OFF"
    if tenant_override in VALID_MODES and tenant_override != "OFF":
        mode = tenant_override
    if degraded and mode in ("FULL", "REDACTED"):
        return "METADATA"
    return mode
