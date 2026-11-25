"""审计写入服务。

审计是旁路能力：任何失败都只记录指标与日志，绝不阻断主业务流程。
"""

import logging

from app.core.metrics import audit_write_total
from app.core.middleware import request_id_var
from app.core.tenancy import current_tenant_id_or_none
from app.models.audit import AuditLog

logger = logging.getLogger(__name__)


class AuditService:
    @staticmethod
    async def write(
        session,
        *,
        action: str,
        resource_type: str,
        resource_id: int | None = None,
        detail: dict | None = None,
        user=None,
        ip: str | None = None,
        user_agent: str | None = None,
        trace_id: str | None = None,
    ) -> AuditLog | None:
        tenant_id = getattr(user, "tenant_id", None) if user is not None else None
        if tenant_id is None:
            tenant_id = current_tenant_id_or_none()
        if tenant_id is None:
            audit_write_total.labels(action=action, result="failed").inc()
            logger.warning("audit write skipped: tenant_id unavailable (action=%s)", action)
            return None

        resolved_trace_id = trace_id if trace_id is not None else request_id_var.get()
        try:
            row = AuditLog(
                tenant_id=tenant_id,
                user_id=getattr(user, "id", None) if user is not None else None,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                ip=ip,
                user_agent=user_agent,
                detail=detail,
                trace_id=resolved_trace_id,
            )
            session.add(row)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响主流程
            audit_write_total.labels(action=action, result="failed").inc()
            logger.warning("audit write failed (action=%s): %s", action, exc)
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 - 回滚失败也无需影响调用方
                pass
            return None

        audit_write_total.labels(action=action, result="success").inc()
        return row
