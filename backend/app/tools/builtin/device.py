"""设备类内置工具：设备状态与遥测。

数据源为真实表 ``devices`` / ``edge_nodes``；遥测表尚未随 MVP 落库，
因此 ``query_device_telemetry`` 诚实返回空结果并说明数据源。
"""

from sqlalchemy import select

from app.models.device import Device, EdgeNode
from app.tools.builtin.common import empty_payload, iso, query_scope
from app.tools.registry import ToolContext, ToolRegistry

STATUS_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "device_id": {"type": "integer"},
        "node_key": {"type": "string", "minLength": 1},
        "status": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}

STATUS_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["items", "count"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "count": {"type": "integer"},
        "note": {"type": "string"},
    },
}

TELEMETRY_INPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["device_id"],
    "properties": {
        "device_id": {"type": "integer"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
    "additionalProperties": False,
}


async def query_device_status(args: dict, ctx: ToolContext | None) -> dict:
    """查询设备在线状态（可按 device_id / node_key 过滤）。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note)

        stmt = (
            select(Device, EdgeNode)
            .outerjoin(
                EdgeNode,
                (EdgeNode.id == Device.node_id) & (EdgeNode.tenant_id == scope.tenant_id),
            )
            .where(Device.tenant_id == scope.tenant_id)
            .order_by(Device.id)
        )
        if args.get("device_id") is not None:
            stmt = stmt.where(Device.id == args["device_id"])
        if args.get("node_key"):
            stmt = stmt.where(EdgeNode.node_key == args["node_key"])
        if args.get("status"):
            stmt = stmt.where(Device.status == args["status"])
        stmt = stmt.limit(int(args.get("limit") or 100))

        rows = (await scope.session.execute(stmt)).all()
        items = [
            {
                "device_id": device.id,
                "name": device.name,
                "kind": device.kind,
                "status": device.status,
                "last_seen_at": iso(device.last_seen_at),
                "node": None
                if node is None
                else {
                    "node_key": node.node_key,
                    "name": node.name,
                    "status": node.status,
                },
            }
            for device, node in rows
        ]
        return {"items": items, "count": len(items)}


async def query_device_telemetry(args: dict, ctx: ToolContext | None) -> dict:
    """查询设备遥测时间序列（当前数据源尚未落库，返回空结果）。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note, device_id=args.get("device_id"))
        # MVP 未建 telemetry 表（MQTT 接入在 Task 11+），不伪造任何点位数据
        return empty_payload(
            "设备遥测表尚未落库（MQTT 接入后可用），当前无数据",
            device_id=args.get("device_id"),
        )


def register(registry: ToolRegistry) -> None:
    registry.register(
        "query_device_status",
        "1.0.0",
        STATUS_INPUT_SCHEMA,
        STATUS_OUTPUT_SCHEMA,
        query_device_status,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询 devices + edge_nodes 中设备的在线状态与所属节点",
    )
    registry.register(
        "query_device_telemetry",
        "1.0.0",
        TELEMETRY_INPUT_SCHEMA,
        STATUS_OUTPUT_SCHEMA,
        query_device_telemetry,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询设备遥测序列（遥测表未落库时返回空结果及说明）",
    )
