"""Prompt 执行记录：按采集模式决定落库内容。

观测不得影响业务：
- 没有租户上下文时直接返回 None（不写库、不抛）。
- 任何写库异常都 rollback + warning + 返回 None。

采集模式语义：
- OFF：只存标识（tenant/trace/span/template/version/status/model_name）。
- METADATA：只存 prompt_hash（未传则按原始 messages 计算），不存内容。
- REDACTED：内容脱敏后存；prompt_hash 始终按原始 messages 计算。
- FULL：内容原样存；prompt_hash 未传则计算。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import TenantNotSetError, get_tenant_id
from app.observability.capture import resolve_mode
from app.observability.models import PromptExecution
from app.observability.prompt.renderer import _hash
from app.observability.redaction import redact

logger = logging.getLogger(__name__)


async def record_execution(
    session: AsyncSession,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
    template_id: int | None,
    version: int | None,
    variables: dict | None = None,
    messages: list[dict] | None = None,
    status: str = "ok",
    prompt_hash: str | None = None,
    model_name: str | None = None,
) -> PromptExecution | None:
    try:
        tenant_id = get_tenant_id()
    except TenantNotSetError:
        return None

    mode = resolve_mode(get_settings().obs_capture_mode)

    hash_value = prompt_hash
    if hash_value is None and messages is not None:
        hash_value = _hash(messages)

    if mode == "OFF":
        stored_variables = None
        stored_messages = None
        hash_value = None
    elif mode == "METADATA":
        stored_variables = None
        stored_messages = None
    elif mode == "REDACTED":
        stored_variables = redact(variables, "REDACTED")
        stored_messages = redact(messages, "REDACTED")
    else:  # FULL
        stored_variables = variables
        stored_messages = messages

    row = PromptExecution(
        tenant_id=tenant_id,
        trace_id=trace_id,
        span_id=span_id,
        template_id=template_id,
        template_version=version,
        variables=stored_variables,
        rendered_messages=stored_messages,
        prompt_hash=hash_value,
        render_status=status,
        model_name=model_name,
    )
    try:
        session.add(row)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.warning("prompt_execution_record_failed", exc_info=True)
        return None
    return row
