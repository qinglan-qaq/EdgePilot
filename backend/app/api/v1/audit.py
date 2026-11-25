"""审计日志查询端点（仅 admin）。"""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role
from app.core.pagination import PageParams, PageResult
from app.core.tenancy import get_tenant_id
from app.db import get_db
from app.models.audit import AuditLog
from app.schemas.common import ok

router = APIRouter(prefix="/audit-logs", tags=["audit"])


def _serialize(row: AuditLog) -> dict:
    return {
        "id": row.id,
        "tenant_id": str(row.tenant_id),
        "user_id": row.user_id,
        "action": row.action,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "ip": row.ip,
        "user_agent": row.user_agent,
        "detail": row.detail,
        "trace_id": row.trace_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("")
async def list_audit_logs(
    params: PageParams = Depends(),
    action: str | None = None,
    resource_type: str | None = None,
    _=Depends(require_role("admin")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    filters = [AuditLog.tenant_id == get_tenant_id()]
    if action is not None:
        filters.append(AuditLog.action == action)
    if resource_type is not None:
        filters.append(AuditLog.resource_type == resource_type)

    total = await session.scalar(
        select(func.count()).select_from(AuditLog).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset(params.offset)
            .limit(params.size)
        )
    ).all()

    page = PageResult(
        items=[_serialize(row) for row in rows],
        total=total or 0,
        page=params.page,
        size=params.size,
    )
    return ok(page.model_dump())
