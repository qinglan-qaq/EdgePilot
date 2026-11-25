"""0005_obs_analytics_indexes: 分析查询所需的 (tenant_id, time) 索引

设计稿 §8 要求统计查询全部走 ``(tenant_id, 时间)`` 索引。原有索引覆盖了
trace 列表、按 span_type/tool_id/agent_id 的筛选，但缺少：

- ``spans(tenant_id, start_time)``：全量 span 计数、错误分布；
- ``spans(tenant_id, parent_span_id)``：Agent 工具使用量按父子关系递归；
- ``llm_calls(tenant_id, created_at)`` / ``tool_calls(tenant_id, created_at)``：
  token、成本、权限拒绝等按时间窗的聚合。

本 revision 只加索引，不改表结构。原计划留给 P8 的 ``0005_evaluation`` 顺延为
``0006_evaluation``，``down_revision`` 链保持单线。
"""

from alembic import op

revision = "0005_obs_analytics_indexes"
down_revision = "0004_obs_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_spans_tenant_start", "spans", ["tenant_id", "start_time"])
    op.create_index(
        "ix_spans_tenant_parent", "spans", ["tenant_id", "parent_span_id"]
    )
    op.create_index(
        "ix_llm_calls_tenant_created", "llm_calls", ["tenant_id", "created_at"]
    )
    op.create_index(
        "ix_tool_calls_tenant_created", "tool_calls", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_tool_calls_tenant_created", table_name="tool_calls")
    op.drop_index("ix_llm_calls_tenant_created", table_name="llm_calls")
    op.drop_index("ix_spans_tenant_parent", table_name="spans")
    op.drop_index("ix_spans_tenant_start", table_name="spans")
