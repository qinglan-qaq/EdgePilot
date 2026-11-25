"""观测分析聚合服务：所有统计都在 SQL 内完成。

口径与设计稿 §8 一致：

- Trace 成功率 = ``status='succeeded'`` / 已结束 trace（``status <> 'running'``）。
- 失败桶 = ``status IN ('failed', 'timeout', 'cancelled')``。
- 延迟分位用 ``percentile_cont`` 直接在 PG 内计算，无 duration 数据时返回 ``NULL``。
- Token 总量 = ``SUM(llm_calls.total_tokens)``，``NULL`` 不计入；全部调用都缺
  usage 时返回 ``NULL`` 而不是 0。``cached_ratio`` 为 ``cached_tokens /
  total_tokens``，分母缺失或为 0 时返回 ``NULL``。
- 成本 = ``SUM(llm_calls.cost)``；价格为 ``NULL`` 的调用只计入
  ``cost_unavailable_count``，**绝不估算**。全部调用缺价时总成本为 ``NULL``。
- Agent 执行数 = 逻辑 span（``span_type='agent' AND attempt=1``）；重试不计入。
- Agent 工具调用数 = 该 agent span 及其后代 span 上的 ``tool_calls``（工具调用
  正常挂在 tool span 上，递归覆盖嵌套工具；同时兼容直接挂在 agent span 的旧数据）。
- Tool 成功率 = 已结束逻辑 tool span（``span_type='tool' AND attempt=1``）中
  ``succeeded`` 占比。
- 权限拒绝数 = ``tool_calls.permission_result='denied'``。
- 错误分布按 ``spans.error->>'code'`` 分组；JSONB 的 ``null`` 与 SQL ``NULL``
  都表示"无错误"，只有确有 error 对象（含空对象）的 span 参与，error 里没有
  code（或 code 为空串）归入 ``unknown``。``llm_calls.error`` 的模型错误数同理。
- 慢请求按 trace ``duration_ms`` 降序。

时间规则：traces / spans 按 ``start_time``，llm_calls / tool_calls 按 ``created_at``。
每条查询都带 ``tenant_id``，跨租户数据不会出现在结果里。

列表上限（避免大租户返回无界结果）：

- overview: error / slowest 各 10，model / agent / tool 分布各 20。
- 独立端点 /agents /tools /models 各 50。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_id

DEFAULT_WINDOW = timedelta(hours=24)
HOURLY_BUCKET_MAX = timedelta(hours=72)

TOP_ERRORS = 10
TOP_SLOWEST_TRACES = 10
OVERVIEW_DISTRIBUTION_LIMIT = 20
ENDPOINT_LIMIT = 50

_FAILED_SQL = "('failed', 'timeout', 'cancelled')"


@dataclass(frozen=True)
class AnalyticsWindow:
    """解析后的查询窗口；``bucket`` 为 ``hour`` 或 ``day``。"""

    start_from: datetime
    start_to: datetime
    bucket: str


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_window(
    start_from: datetime | None,
    start_to: datetime | None,
    *,
    now: datetime | None = None,
) -> AnalyticsWindow:
    """补全默认窗口并决定趋势粒度。

    默认最近 24 小时（UTC）；只给 ``start_to`` 时向前推 24 小时，只给
    ``start_from`` 时结束于当前时间。窗口 <= 72 小时按小时分桶，否则按天。
    """
    end = _as_utc(start_to) if start_to is not None else _as_utc(
        now or datetime.now(timezone.utc)
    )
    start = _as_utc(start_from) if start_from is not None else end - DEFAULT_WINDOW
    if start > end:
        raise ValueError("start_from 不能晚于 start_to")
    bucket = "hour" if end - start <= HOURLY_BUCKET_MAX else "day"
    return AnalyticsWindow(start_from=start, start_to=end, bucket=bucket)


def _params(window: AnalyticsWindow) -> dict:
    return {
        "tenant_id": get_tenant_id(),
        "start_from": window.start_from,
        "start_to": window.start_to,
    }


def _as_float(value) -> float | None:
    return None if value is None else float(value)


def _as_int(value) -> int | None:
    return None if value is None else int(value)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if not denominator else numerator / denominator


def _bucket_value(value: datetime | None) -> datetime | None:
    """date_trunc 返回不带时区的 timestamp；统一标记为 UTC。"""
    if value is None:
        return None
    return _as_utc(value)


# ------------------------------------------------------------------ SQL 片段

_TRACE_CARDS_SQL = f"""
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE status <> 'running') AS terminal,
    COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded,
    COUNT(*) FILTER (WHERE status IN {_FAILED_SQL}) AS failed,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p50_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p95_ms
FROM traces
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
"""

_SPAN_CARDS_SQL = """
SELECT
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE span_type = 'agent' AND attempt = 1) AS agent_runs,
    COUNT(*) FILTER (
        WHERE span_type = 'tool' AND attempt = 1 AND status <> 'running'
    ) AS tool_terminal,
    COUNT(*) FILTER (
        WHERE span_type = 'tool' AND attempt = 1 AND status = 'succeeded'
    ) AS tool_succeeded
FROM spans
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
"""

_LLM_CARDS_SQL = """
SELECT
    COUNT(*) AS calls,
    SUM(total_tokens) AS total_tokens,
    SUM(cost) AS total_cost,
    COUNT(*) FILTER (WHERE cost IS NULL) AS cost_unavailable
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
"""

_TOKEN_TOTALS_SQL = """
SELECT
    COUNT(*) AS calls,
    SUM(prompt_tokens) AS prompt_tokens,
    SUM(completion_tokens) AS completion_tokens,
    SUM(cached_tokens) AS cached_tokens,
    SUM(total_tokens) AS total_tokens,
    COUNT(*) FILTER (WHERE total_tokens IS NULL) AS unavailable_calls
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
"""

_COVERAGE_SQL = """
SELECT
    COUNT(*) AS total_models,
    COUNT(*) FILTER (WHERE priced) AS priced_models
FROM (
    SELECT provider, model_name, bool_or(cost IS NOT NULL) AS priced
    FROM llm_calls
    WHERE tenant_id = :tenant_id
      AND created_at >= :start_from
      AND created_at <= :start_to
    GROUP BY provider, model_name
) AS models
"""

_PERMISSION_DENIED_SQL = """
SELECT COUNT(*) AS denied
FROM tool_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
  AND permission_result = 'denied'
"""

_TRACE_TREND_SQL = f"""
SELECT
    date_trunc(CAST(:bucket AS text), start_time AT TIME ZONE 'UTC') AS bucket,
    COUNT(*) AS total,
    COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded,
    COUNT(*) FILTER (WHERE status IN {_FAILED_SQL}) AS failed
FROM traces
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
GROUP BY bucket
ORDER BY bucket
"""

_LATENCY_TREND_SQL = """
SELECT
    date_trunc(CAST(:bucket AS text), start_time AT TIME ZONE 'UTC') AS bucket,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p50_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p95_ms
FROM traces
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
GROUP BY bucket
ORDER BY bucket
"""

_TOKEN_TREND_SQL = """
SELECT
    date_trunc(CAST(:bucket AS text), created_at AT TIME ZONE 'UTC') AS bucket,
    SUM(total_tokens) AS total_tokens,
    SUM(cached_tokens) AS cached_tokens
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
GROUP BY bucket
ORDER BY bucket
"""

_MODEL_DISTRIBUTION_SQL = """
SELECT
    provider,
    model_name,
    COUNT(*) AS calls,
    SUM(total_tokens) AS total_tokens,
    SUM(cost) AS cost,
    COUNT(*) FILTER (WHERE cost IS NULL) AS cost_unavailable_count
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
GROUP BY provider, model_name
ORDER BY calls DESC, provider, model_name
LIMIT :limit
"""

_AGENT_DISTRIBUTION_SQL = f"""
SELECT
    name AS label,
    COUNT(*) AS runs,
    COUNT(*) FILTER (WHERE status IN {_FAILED_SQL}) AS failed,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p95_ms
FROM spans
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
  AND span_type = 'agent'
  AND attempt = 1
GROUP BY name
ORDER BY runs DESC, label
LIMIT :limit
"""

_TOOL_DISTRIBUTION_SQL = """
SELECT
    COALESCE(tool_id, 'unknown') AS tool_id,
    MIN(name) AS tool_name,
    COUNT(*) AS calls,
    COUNT(*) FILTER (WHERE status <> 'running') AS terminal,
    COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p95_ms
FROM spans
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
  AND span_type = 'tool'
  AND attempt = 1
GROUP BY COALESCE(tool_id, 'unknown')
ORDER BY calls DESC, tool_id
LIMIT :limit
"""

_ERROR_DISTRIBUTION_SQL = """
SELECT
    COALESCE(NULLIF(error ->> 'code', ''), 'unknown') AS code,
    COUNT(*) AS count
FROM spans
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
  AND error IS NOT NULL
  AND error <> 'null'::jsonb
GROUP BY code
ORDER BY count DESC, code
LIMIT :limit
"""

_SLOWEST_TRACES_SQL = """
SELECT trace_id, name, status, duration_ms, start_time
FROM traces
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
  AND duration_ms IS NOT NULL
ORDER BY duration_ms DESC, start_time DESC, trace_id
LIMIT :limit
"""

_SPAN_LATENCY_SQL = """
SELECT
    span_type,
    COUNT(*) AS count,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p50_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
        FILTER (WHERE duration_ms IS NOT NULL) AS p95_ms
FROM spans
WHERE tenant_id = :tenant_id
  AND start_time >= :start_from
  AND start_time <= :start_to
GROUP BY span_type
ORDER BY count DESC, span_type
LIMIT :limit
"""

_TOKEN_BY_MODEL_SQL = """
SELECT
    provider,
    model_name,
    COUNT(*) AS calls,
    SUM(prompt_tokens) AS prompt_tokens,
    SUM(completion_tokens) AS completion_tokens,
    SUM(cached_tokens) AS cached_tokens,
    SUM(total_tokens) AS total_tokens
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
GROUP BY provider, model_name
ORDER BY calls DESC, provider, model_name
LIMIT :limit
"""

_COST_BY_MODEL_SQL = """
SELECT
    provider,
    model_name,
    COUNT(*) AS calls,
    SUM(cost) AS cost,
    COUNT(*) FILTER (WHERE cost IS NULL) AS cost_unavailable_count
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
GROUP BY provider, model_name
ORDER BY cost DESC NULLS LAST, calls DESC, provider, model_name
LIMIT :limit
"""

_AGENTS_SQL = f"""
WITH RECURSIVE agent_spans AS (
    SELECT span_id, agent_id, name, status, duration_ms
    FROM spans
    WHERE tenant_id = :tenant_id
      AND start_time >= :start_from
      AND start_time <= :start_to
      AND span_type = 'agent'
      AND attempt = 1
),
descendants AS (
    SELECT
        a.span_id AS agent_span_id,
        a.span_id AS span_id,
        'agent'::text AS span_type,
        0 AS depth
    FROM agent_spans AS a
    UNION ALL
    SELECT
        d.agent_span_id,
        s.span_id,
        s.span_type,
        d.depth + 1
    FROM descendants AS d
    JOIN spans AS s
      ON s.parent_span_id = d.span_id
     AND s.tenant_id = :tenant_id
    WHERE d.depth < 32
),
llm AS (
    SELECT span_id, SUM(total_tokens) AS total_tokens
    FROM llm_calls
    WHERE tenant_id = :tenant_id
      AND created_at >= :start_from
      AND created_at <= :start_to
    GROUP BY span_id
),
tools AS (
    SELECT d.agent_span_id AS span_id, COUNT(*) AS tool_calls
    FROM tool_calls AS tc
    JOIN descendants AS d ON d.span_id = tc.span_id
    WHERE tc.tenant_id = :tenant_id
      AND tc.created_at >= :start_from
      AND tc.created_at <= :start_to
      -- 工具调用正常挂在 tool span 上；也兼容直接挂在 agent span 的旧数据。
      AND (d.span_type = 'tool' OR d.span_id = d.agent_span_id)
    GROUP BY d.agent_span_id
)
SELECT
    a.agent_id,
    a.name AS label,
    COUNT(*) AS runs,
    COUNT(*) FILTER (WHERE a.status IN {_FAILED_SQL}) AS failed,
    COUNT(*) FILTER (WHERE a.status = 'succeeded') AS succeeded,
    COUNT(*) FILTER (WHERE a.status <> 'running') AS terminal,
    AVG(a.duration_ms) AS avg_duration_ms,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY a.duration_ms)
        FILTER (WHERE a.duration_ms IS NOT NULL) AS p95_duration_ms,
    SUM(l.total_tokens) AS total_tokens,
    COALESCE(SUM(t.tool_calls), 0) AS tool_calls
FROM agent_spans AS a
LEFT JOIN llm AS l ON l.span_id = a.span_id
LEFT JOIN tools AS t ON t.span_id = a.span_id
GROUP BY a.agent_id, a.name
ORDER BY runs DESC, a.name, a.agent_id
LIMIT :limit
"""

_TOOLS_SQL = """
WITH tool_spans AS (
    SELECT
        COALESCE(tool_id, 'unknown') AS tool_id,
        MIN(name) AS tool_name,
        COUNT(*) AS calls,
        COUNT(*) FILTER (WHERE status <> 'running') AS terminal,
        COUNT(*) FILTER (WHERE status = 'succeeded') AS succeeded,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms)
            FILTER (WHERE duration_ms IS NOT NULL) AS p50_ms,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)
            FILTER (WHERE duration_ms IS NOT NULL) AS p95_ms,
        COALESCE(SUM(retry_count), 0) AS retry_count
    FROM spans
    WHERE tenant_id = :tenant_id
      AND start_time >= :start_from
      AND start_time <= :start_to
      AND span_type = 'tool'
      AND attempt = 1
    GROUP BY COALESCE(tool_id, 'unknown')
),
denied AS (
    SELECT COALESCE(tool_id, 'unknown') AS tool_id, COUNT(*) AS denied
    FROM tool_calls
    WHERE tenant_id = :tenant_id
      AND created_at >= :start_from
      AND created_at <= :start_to
      AND permission_result = 'denied'
    GROUP BY COALESCE(tool_id, 'unknown')
)
SELECT
    s.tool_id,
    s.tool_name,
    s.calls,
    s.terminal,
    s.succeeded,
    s.p50_ms,
    s.p95_ms,
    s.retry_count,
    COALESCE(d.denied, 0) AS permission_denied
FROM tool_spans AS s
LEFT JOIN denied AS d USING (tool_id)
ORDER BY s.calls DESC, s.tool_id
LIMIT :limit
"""

_MODELS_SQL = """
SELECT
    provider,
    model_name,
    COUNT(*) AS calls,
    SUM(prompt_tokens) AS prompt_tokens,
    SUM(completion_tokens) AS completion_tokens,
    SUM(cached_tokens) AS cached_tokens,
    SUM(total_tokens) AS total_tokens,
    SUM(cost) AS cost,
    COUNT(*) FILTER (WHERE cost IS NULL) AS cost_unavailable_count,
    COUNT(*) FILTER (
        WHERE error IS NOT NULL AND error <> 'null'::jsonb
    ) AS errors
FROM llm_calls
WHERE tenant_id = :tenant_id
  AND created_at >= :start_from
  AND created_at <= :start_to
GROUP BY provider, model_name
ORDER BY calls DESC, provider, model_name
LIMIT :limit
"""


# ------------------------------------------------------------------ 服务函数

async def _trace_cards(session: AsyncSession, params: dict) -> dict:
    return dict(
        (await session.execute(text(_TRACE_CARDS_SQL), params)).mappings().one()
    )


async def overview(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    trace_row = await _trace_cards(session, params)
    span_row = dict(
        (await session.execute(text(_SPAN_CARDS_SQL), params)).mappings().one()
    )
    llm_row = dict(
        (await session.execute(text(_LLM_CARDS_SQL), params)).mappings().one()
    )
    denied = await session.scalar(text(_PERMISSION_DENIED_SQL), params) or 0
    coverage = dict(
        (await session.execute(text(_COVERAGE_SQL), params)).mappings().one()
    )

    trace_trend = [
        {
            "bucket": _bucket_value(row["bucket"]),
            "total": int(row["total"]),
            "succeeded": int(row["succeeded"]),
            "failed": int(row["failed"]),
        }
        for row in (
            await session.execute(
                text(_TRACE_TREND_SQL), {**params, "bucket": window.bucket}
            )
        ).mappings()
    ]
    latency_trend = [
        {
            "bucket": _bucket_value(row["bucket"]),
            "p50_ms": _as_float(row["p50_ms"]),
            "p95_ms": _as_float(row["p95_ms"]),
        }
        for row in (
            await session.execute(
                text(_LATENCY_TREND_SQL), {**params, "bucket": window.bucket}
            )
        ).mappings()
    ]
    token_trend = [
        {
            "bucket": _bucket_value(row["bucket"]),
            "total_tokens": _as_int(row["total_tokens"]),
            "cached_tokens": _as_int(row["cached_tokens"]),
        }
        for row in (
            await session.execute(
                text(_TOKEN_TREND_SQL), {**params, "bucket": window.bucket}
            )
        ).mappings()
    ]
    model_distribution = [
        {
            "provider": row["provider"],
            "model_name": row["model_name"],
            "calls": int(row["calls"]),
            "total_tokens": _as_int(row["total_tokens"]),
            "cost": _as_float(row["cost"]),
            "cost_unavailable_count": int(row["cost_unavailable_count"]),
        }
        for row in (
            await session.execute(
                text(_MODEL_DISTRIBUTION_SQL),
                {**params, "limit": OVERVIEW_DISTRIBUTION_LIMIT},
            )
        ).mappings()
    ]
    agent_distribution = [
        {
            "label": row["label"],
            "runs": int(row["runs"]),
            "failed": int(row["failed"]),
            "p95_ms": _as_float(row["p95_ms"]),
        }
        for row in (
            await session.execute(
                text(_AGENT_DISTRIBUTION_SQL),
                {**params, "limit": OVERVIEW_DISTRIBUTION_LIMIT},
            )
        ).mappings()
    ]
    tool_distribution = [
        {
            "tool_id": row["tool_id"],
            "tool_name": row["tool_name"],
            "calls": int(row["calls"]),
            "success_rate": _rate(
                int(row["succeeded"]), int(row["terminal"])
            ),
            "p95_ms": _as_float(row["p95_ms"]),
        }
        for row in (
            await session.execute(
                text(_TOOL_DISTRIBUTION_SQL),
                {**params, "limit": OVERVIEW_DISTRIBUTION_LIMIT},
            )
        ).mappings()
    ]
    error_distribution = [
        {"code": row["code"], "count": int(row["count"])}
        for row in (
            await session.execute(
                text(_ERROR_DISTRIBUTION_SQL),
                {**params, "limit": TOP_ERRORS},
            )
        ).mappings()
    ]
    slowest_traces = [
        {
            "trace_id": row["trace_id"],
            "name": row["name"],
            "status": row["status"],
            "duration_ms": _as_float(row["duration_ms"]),
            "start_time": row["start_time"],
        }
        for row in (
            await session.execute(
                text(_SLOWEST_TRACES_SQL),
                {**params, "limit": TOP_SLOWEST_TRACES},
            )
        ).mappings()
    ]

    return {
        "window": {
            "start_from": window.start_from,
            "start_to": window.start_to,
            "bucket": window.bucket,
        },
        "cards": {
            "traces_total": int(trace_row["total"]),
            "trace_success_rate": _rate(
                int(trace_row["succeeded"]), int(trace_row["terminal"])
            ),
            "trace_p95_ms": _as_float(trace_row["p95_ms"]),
            "spans_total": int(span_row["total"]),
            "agent_runs": int(span_row["agent_runs"]),
            "tool_success_rate": _rate(
                int(span_row["tool_succeeded"]), int(span_row["tool_terminal"])
            ),
            "tool_permission_denied": int(denied),
            "llm_calls": int(llm_row["calls"]),
            "total_tokens": (
                None
                if llm_row["total_tokens"] is None
                else int(llm_row["total_tokens"])
            ),
            "total_cost": _as_float(llm_row["total_cost"]),
            "cost_unavailable_count": int(llm_row["cost_unavailable"]),
            "cost_coverage": {
                "priced_models": int(coverage["priced_models"]),
                "total_models": int(coverage["total_models"]),
            },
        },
        "charts": {
            "trace_trend": trace_trend,
            "latency_trend": latency_trend,
            "token_trend": token_trend,
            "model_distribution": model_distribution,
            "agent_distribution": agent_distribution,
            "tool_distribution": tool_distribution,
            "error_distribution": error_distribution,
            "slowest_traces": slowest_traces,
        },
    }


async def latency(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    trace_row = await _trace_cards(session, params)
    by_span_type = [
        {
            "span_type": row["span_type"],
            "count": int(row["count"]),
            "p50_ms": _as_float(row["p50_ms"]),
            "p95_ms": _as_float(row["p95_ms"]),
        }
        for row in (
            await session.execute(
                text(_SPAN_LATENCY_SQL),
                {**params, "limit": ENDPOINT_LIMIT},
            )
        ).mappings()
    ]
    return {
        "trace": {
            "count": int(trace_row["total"]),
            "p50_ms": _as_float(trace_row["p50_ms"]),
            "p95_ms": _as_float(trace_row["p95_ms"]),
        },
        "by_span_type": by_span_type,
    }


async def tokens(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    row = dict(
        (await session.execute(text(_TOKEN_TOTALS_SQL), params)).mappings().one()
    )
    total_tokens = _as_int(row["total_tokens"])
    cached_tokens = _as_int(row["cached_tokens"])
    by_model = [
        {
            "provider": m["provider"],
            "model_name": m["model_name"],
            "calls": int(m["calls"]),
            "prompt_tokens": _as_int(m["prompt_tokens"]),
            "completion_tokens": _as_int(m["completion_tokens"]),
            "cached_tokens": _as_int(m["cached_tokens"]),
            "total_tokens": _as_int(m["total_tokens"]),
        }
        for m in (
            await session.execute(
                text(_TOKEN_BY_MODEL_SQL),
                {**params, "limit": ENDPOINT_LIMIT},
            )
        ).mappings()
    ]
    return {
        "total_tokens": total_tokens,
        "prompt_tokens": _as_int(row["prompt_tokens"]),
        "completion_tokens": _as_int(row["completion_tokens"]),
        "cached_tokens": cached_tokens,
        "cached_ratio": (
            None
            if not total_tokens or cached_tokens is None
            else cached_tokens / total_tokens
        ),
        "unavailable_calls": int(row["unavailable_calls"]),
        "by_model": by_model,
    }


async def cost(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    coverage = dict(
        (await session.execute(text(_COVERAGE_SQL), params)).mappings().one()
    )
    llm = dict(
        (await session.execute(text(_LLM_CARDS_SQL), params)).mappings().one()
    )
    by_model = [
        {
            "provider": row["provider"],
            "model_name": row["model_name"],
            "calls": int(row["calls"]),
            "cost": _as_float(row["cost"]),
            "cost_unavailable_count": int(row["cost_unavailable_count"]),
        }
        for row in (
            await session.execute(
                text(_COST_BY_MODEL_SQL),
                {**params, "limit": ENDPOINT_LIMIT},
            )
        ).mappings()
    ]
    return {
        "total_cost": _as_float(llm["total_cost"]),
        "cost_unavailable_count": int(llm["cost_unavailable"]),
        "coverage": {
            "priced_models": int(coverage["priced_models"]),
            "total_models": int(coverage["total_models"]),
        },
        "by_model": by_model,
    }


async def agents(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    items = []
    for row in (
        await session.execute(
            text(_AGENTS_SQL), {**params, "limit": ENDPOINT_LIMIT}
        )
    ).mappings():
        items.append(
            {
                "agent_id": row["agent_id"],
                "label": row["label"],
                "runs": int(row["runs"]),
                "failed": int(row["failed"]),
                "success_rate": _rate(
                    int(row["succeeded"]), int(row["terminal"])
                ),
                "avg_duration_ms": _as_float(row["avg_duration_ms"]),
                "p95_duration_ms": _as_float(row["p95_duration_ms"]),
                "total_tokens": _as_int(row["total_tokens"]),
                "tool_calls": int(row["tool_calls"]),
            }
        )
    return {"items": items}


async def tools(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    items = [
        {
            "tool_id": row["tool_id"],
            "tool_name": row["tool_name"],
            "calls": int(row["calls"]),
            "success_rate": _rate(
                int(row["succeeded"]), int(row["terminal"])
            ),
            "p50_ms": _as_float(row["p50_ms"]),
            "p95_ms": _as_float(row["p95_ms"]),
            "retry_count": int(row["retry_count"]),
            "permission_denied": int(row["permission_denied"]),
        }
        for row in (
            await session.execute(
                text(_TOOLS_SQL), {**params, "limit": ENDPOINT_LIMIT}
            )
        ).mappings()
    ]
    return {"items": items}


async def models(session: AsyncSession, window: AnalyticsWindow) -> dict:
    params = _params(window)
    items = [
        {
            "provider": row["provider"],
            "model_name": row["model_name"],
            "calls": int(row["calls"]),
            "prompt_tokens": _as_int(row["prompt_tokens"]),
            "completion_tokens": _as_int(row["completion_tokens"]),
            "cached_tokens": _as_int(row["cached_tokens"]),
            "total_tokens": _as_int(row["total_tokens"]),
            "cost": _as_float(row["cost"]),
            "cost_unavailable_count": int(row["cost_unavailable_count"]),
            "errors": int(row["errors"]),
        }
        for row in (
            await session.execute(
                text(_MODELS_SQL), {**params, "limit": ENDPOINT_LIMIT}
            )
        ).mappings()
    ]
    return {"items": items}
