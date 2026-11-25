"""可观测设置只读服务：采集模式、保留期、脱敏规则与缺价模型。

本期不提供任何写入路径——采集模式由环境变量 ``OBS_CAPTURE_MODE`` 控制，
前端只能如实展示当前值并提示“修改需改环境变量并重启”。所有查询都限定
``tenant_id``；没有数据的租户返回空列表，绝不补齐占位行。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import get_tenant_id
from app.observability.redaction import SENSITIVE_KEY_RE

# 采集模式枚举，顺序与前端展示一致。
CAPTURE_MODES = ["OFF", "METADATA", "REDACTED", "FULL"]

# 保留期（天）。本期为固定策略，尚未提供运行时修改接口。
RETENTION_DAYS = {
    "trace_events": 90,
    "traces": 180,
    "spans": 180,
    "llm_calls": 180,
    "tool_calls": 180,
    "retrieval_records": 180,
    "prompt_executions": 180,
    "audit_logs": 365,
}

# 与 redaction._scrub 的正则一一对应；顺序固定，便于前端顺序展示。
SCRUBBERS = ["bearer", "sk_key", "phone", "id_card"]

UNPRICED_MODELS_LIMIT = 20

_UNPRICED_MODELS_SQL = """
SELECT
    provider,
    model_name,
    COUNT(*) AS calls
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND cost IS NULL
GROUP BY provider, model_name
ORDER BY calls DESC, provider, model_name
LIMIT :limit
"""


async def unpriced_models(session: AsyncSession) -> list[dict]:
    """租户范围内 ``cost IS NULL`` 的调用，按 provider/model 聚合后降序。

    只统计缺价组合；没有缺价数据时返回空列表。
    """
    rows = (
        await session.execute(
            text(_UNPRICED_MODELS_SQL),
            {"tenant_id": get_tenant_id(), "limit": UNPRICED_MODELS_LIMIT},
        )
    ).mappings()
    return [
        {
            "provider": row["provider"],
            "model_name": row["model_name"],
            "calls": int(row["calls"]),
        }
        for row in rows
    ]


async def get_settings_view(session: AsyncSession) -> dict:
    """组合只读设置视图；``capture_mode`` 直接来自当前进程配置。"""
    settings = get_settings()
    return {
        "capture_mode": settings.obs_capture_mode,
        "capture_modes": list(CAPTURE_MODES),
        "retention_days": dict(RETENTION_DAYS),
        "redaction": {
            "sensitive_key_pattern": SENSITIVE_KEY_RE.pattern,
            "scrubbers": list(SCRUBBERS),
            "token_count_exempt": True,
        },
        "unpriced_models": await unpriced_models(session),
    }
