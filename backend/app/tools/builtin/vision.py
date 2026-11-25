"""视觉类内置工具：视觉事件与告警事件。

两者都读真实表 ``vision_events``；告警没有独立表，按事件字段定义：
``severity in ('high', 'critical')`` 或 ``event_type = 'anomaly'``。
"""

from sqlalchemy import or_, select

from app.models.event import VisionEvent
from app.tools.builtin.common import empty_payload, iso, query_scope, to_jsonable
from app.tools.registry import ToolContext, ToolRegistry

EVENTS_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "device_id": {"type": "integer"},
        "node_id": {"type": "integer"},
        "event_type": {"type": "string", "minLength": 1},
        "severity": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}

EVENTS_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["items", "count"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "count": {"type": "integer"},
        "note": {"type": "string"},
    },
}

ALARMS_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "device_id": {"type": "integer"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}


def _event_item(event: VisionEvent) -> dict:
    return {
        "event_id": event.id,
        "node_id": event.node_id,
        "device_id": event.device_id,
        "event_type": event.event_type,
        "severity": event.severity,
        "occurred_at": iso(event.occurred_at),
        "confidence": to_jsonable(event.confidence),
        "track_id": event.track_id,
        "snapshot_path": event.snapshot_path,
        "model_version": event.model_version,
        "status": event.status,
    }


async def query_vision_events(args: dict, ctx: ToolContext | None) -> dict:
    """查询视觉事件（按设备/节点/事件类型/级别过滤）。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note)

        stmt = (
            select(VisionEvent)
            .where(VisionEvent.tenant_id == scope.tenant_id)
            .order_by(VisionEvent.occurred_at.desc())
        )
        if args.get("device_id") is not None:
            stmt = stmt.where(VisionEvent.device_id == args["device_id"])
        if args.get("node_id") is not None:
            stmt = stmt.where(VisionEvent.node_id == args["node_id"])
        if args.get("event_type"):
            stmt = stmt.where(VisionEvent.event_type == args["event_type"])
        if args.get("severity"):
            stmt = stmt.where(VisionEvent.severity == args["severity"])
        stmt = stmt.limit(int(args.get("limit") or 50))

        events = (await scope.session.scalars(stmt)).all()
        items = [_event_item(event) for event in events]
        return {"items": items, "count": len(items)}


async def query_alarm_events(args: dict, ctx: ToolContext | None) -> dict:
    """查询告警事件（vision_events 中高等级或 anomaly 事件）。"""
    async with query_scope(ctx) as scope:
        note = "数据源 vision_events：severity in ('high','critical') 或 event_type='anomaly'"
        if scope.session is None:
            return empty_payload(scope.note)

        stmt = (
            select(VisionEvent)
            .where(
                VisionEvent.tenant_id == scope.tenant_id,
                or_(
                    VisionEvent.severity.in_(("high", "critical")),
                    VisionEvent.event_type == "anomaly",
                ),
            )
            .order_by(VisionEvent.occurred_at.desc())
        )
        if args.get("device_id") is not None:
            stmt = stmt.where(VisionEvent.device_id == args["device_id"])
        stmt = stmt.limit(int(args.get("limit") or 50))

        events = (await scope.session.scalars(stmt)).all()
        items = [_event_item(event) for event in events]
        return {"items": items, "count": len(items), "note": note}


def register(registry: ToolRegistry) -> None:
    registry.register(
        "query_vision_events",
        "1.0.0",
        EVENTS_INPUT_SCHEMA,
        EVENTS_OUTPUT_SCHEMA,
        query_vision_events,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询 vision_events 中的视觉事件（含置信度、快照路径、模型版本）",
    )
    registry.register(
        "query_alarm_events",
        "1.0.0",
        ALARMS_INPUT_SCHEMA,
        EVENTS_OUTPUT_SCHEMA,
        query_alarm_events,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询告警事件（vision_events 中 high/critical 级别或 anomaly 类型）",
    )
