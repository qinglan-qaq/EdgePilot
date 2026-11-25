"""运维类内置工具：边缘节点指标、模型状态、任务历史。"""

from sqlalchemy import select

from app.models.device import EdgeNode
from app.models.model_config import ModelConfig
from app.models.run import WorkflowRun
from app.tools.builtin.common import empty_payload, iso, query_scope
from app.tools.registry import ToolContext, ToolRegistry

LIST_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["items", "count"],
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "count": {"type": "integer"},
        "note": {"type": "string"},
    },
}

NODE_METRICS_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "node_key": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "additionalProperties": False,
}

MODEL_STATUS_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "model_type": {"type": "string", "enum": ["llm", "embedding", "reranker"]},
        "status": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

TASK_HISTORY_INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
    },
    "additionalProperties": False,
}


async def query_edge_node_metrics(args: dict, ctx: ToolContext | None) -> dict:
    """查询边缘节点资源与在线状态（edge_nodes.metrics）。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note)

        stmt = (
            select(EdgeNode)
            .where(EdgeNode.tenant_id == scope.tenant_id)
            .order_by(EdgeNode.node_key)
        )
        if args.get("node_key"):
            stmt = stmt.where(EdgeNode.node_key == args["node_key"])
        stmt = stmt.limit(int(args.get("limit") or 50))

        nodes = (await scope.session.scalars(stmt)).all()
        items = [
            {
                "node_key": node.node_key,
                "name": node.name,
                "status": node.status,
                "os": node.os,
                "arch": node.arch,
                "agent_version": node.agent_version,
                "ip": node.ip,
                "labels": node.labels or {},
                "metrics": node.metrics,
                "last_heartbeat_at": iso(node.last_heartbeat_at),
            }
            for node in nodes
        ]
        return {"items": items, "count": len(items)}


async def query_model_status(args: dict, ctx: ToolContext | None) -> dict:
    """查询模型配置与启用状态；不返回任何密钥字段。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note)

        stmt = (
            select(ModelConfig)
            .where(ModelConfig.tenant_id == scope.tenant_id)
            .order_by(ModelConfig.model_type, ModelConfig.name)
        )
        if args.get("model_type"):
            stmt = stmt.where(ModelConfig.model_type == args["model_type"])
        if args.get("status"):
            stmt = stmt.where(ModelConfig.status == args["status"])

        rows = (await scope.session.scalars(stmt)).all()
        items = [
            {
                "model_config_id": row.id,
                "name": row.name,
                "model_type": row.model_type,
                "provider": row.provider,
                "model_name": row.model_name,
                "deployment_location": row.deployment_location,
                "status": row.status,
            }
            for row in rows
        ]
        return {"items": items, "count": len(items)}


async def query_task_history(args: dict, ctx: ToolContext | None) -> dict:
    """查询工作流任务历史（workflow_runs），可按状态过滤。"""
    async with query_scope(ctx) as scope:
        if scope.session is None:
            return empty_payload(scope.note)

        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.tenant_id == scope.tenant_id)
            .order_by(WorkflowRun.created_at.desc())
        )
        if args.get("status"):
            stmt = stmt.where(WorkflowRun.status == args["status"])
        stmt = stmt.limit(int(args.get("limit") or 20))

        runs = (await scope.session.scalars(stmt)).all()
        items = [
            {
                "run_id": run.id,
                "workflow_version_id": run.workflow_version_id,
                "trigger_type": run.trigger_type,
                "status": run.status,
                "error": run.error,
                "trace_id": run.trace_id,
                "created_at": iso(run.created_at),
                "started_at": iso(run.started_at),
                "finished_at": iso(run.finished_at),
            }
            for run in runs
        ]
        return {"items": items, "count": len(items)}


def register(registry: ToolRegistry) -> None:
    registry.register(
        "query_edge_node_metrics",
        "1.0.0",
        NODE_METRICS_INPUT_SCHEMA,
        LIST_OUTPUT_SCHEMA,
        query_edge_node_metrics,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询 edge_nodes 的节点资源指标与心跳状态",
    )
    registry.register(
        "query_model_status",
        "1.0.0",
        MODEL_STATUS_INPUT_SCHEMA,
        LIST_OUTPUT_SCHEMA,
        query_model_status,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询 model_configs 中模型配置与启用状态（不含密钥）",
    )
    registry.register(
        "query_task_history",
        "1.0.0",
        TASK_HISTORY_INPUT_SCHEMA,
        LIST_OUTPUT_SCHEMA,
        query_task_history,
        permission_scope="read",
        timeout_ms=10000,
        max_retries=1,
        side_effect=False,
        description="查询 workflow_runs 任务历史与失败原因",
    )
