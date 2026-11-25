"""工作流 DSL 层：常量与哈希、受限表达式、结构/数据库校验。

本包只提供 P6 Task 21 的契约能力；执行引擎、Celery 任务与 API 路由
由后续 Task 实现，不在此处引入，避免循环依赖。
"""

from app.workflow.dsl import (
    MAX_NODE_ID_LENGTH,
    NODE_ID_MAX_LENGTH,
    NODE_ID_PATTERN,
    NODE_ID_RE,
    NODE_TYPES,
    RESERVED_NODE_IDS,
    ROLES,
    dsl_hash,
    edges_of,
    is_valid_node_id,
    node_map,
    nodes_of,
)
from app.workflow.expression import (
    CompiledExpression,
    ExpressionError,
    compile_expression,
    evaluate_condition,
    evaluate_expression,
)
from app.workflow.validator import (
    MAX_TIMEOUT_MS,
    ValidationIssue,
    ValidationReport,
    WorkflowValidationError,
    assert_valid,
    validate_dsl,
    validate_workflow,
)

__all__ = [
    "MAX_NODE_ID_LENGTH",
    "MAX_TIMEOUT_MS",
    "NODE_ID_MAX_LENGTH",
    "NODE_ID_PATTERN",
    "NODE_ID_RE",
    "NODE_TYPES",
    "RESERVED_NODE_IDS",
    "ROLES",
    "CompiledExpression",
    "ExpressionError",
    "ValidationIssue",
    "ValidationReport",
    "WorkflowValidationError",
    "assert_valid",
    "compile_expression",
    "dsl_hash",
    "edges_of",
    "evaluate_condition",
    "evaluate_expression",
    "is_valid_node_id",
    "node_map",
    "nodes_of",
    "validate_dsl",
    "validate_workflow",
]
