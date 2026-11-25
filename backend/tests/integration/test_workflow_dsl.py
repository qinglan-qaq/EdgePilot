"""工作流 DSL 的数据库内存在性校验集成测试（Task 21）。

只验证 ``validate_workflow`` 的只读查询语义：同租户 active Agent / enabled Tool
才能通过；跨租户同名数据不可见。所有写入只 flush，依赖外层事务回滚。
"""

from __future__ import annotations

from app.models.agent import Agent
from app.models.tool import Tool
from app.workflow.validator import validate_workflow
from tests.conftest import OTHER_TENANT, TENANT


def _agent_dsl(agent_name: str) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "plan",
                "type": "agent",
                "agent": agent_name,
                "timeout_ms": 30000,
                "input_map": {"question": "$input.question"},
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "plan"},
            {"from": "plan", "to": "end"},
        ],
    }


def _tool_dsl(tool_id: str) -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "timeout_ms": 1000},
            {
                "id": "lookup",
                "type": "tool",
                "tool": tool_id,
                "timeout_ms": 3000,
                "input_map": {"device_id": "$input.device_id"},
            },
            {"id": "end", "type": "end", "timeout_ms": 1000},
        ],
        "edges": [
            {"from": "start", "to": "lookup"},
            {"from": "lookup", "to": "end"},
        ],
    }


def _codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


async def test_active_agent_passes_but_disabled_agent_fails(session):
    session.add_all(
        [
            Agent(
                tenant_id=TENANT,
                name="wf-active-agent",
                agent_type="device",
                status="active",
            ),
            Agent(
                tenant_id=TENANT,
                name="wf-disabled-agent",
                agent_type="device",
                status="disabled",
            ),
        ]
    )
    await session.flush()

    active_report = await validate_workflow(session, TENANT, _agent_dsl("wf-active-agent"))
    disabled_report = await validate_workflow(
        session, TENANT, _agent_dsl("wf-disabled-agent")
    )

    assert active_report.valid is True
    assert disabled_report.valid is False
    issue = next(
        item
        for item in disabled_report.errors
        if item.code == "agent_not_found_or_disabled"
    )
    assert issue.node_id == "plan"


async def test_enabled_tool_passes_but_disabled_tool_fails(session):
    session.add_all(
        [
            Tool(
                tenant_id=TENANT,
                tool_id="wf_enabled_tool",
                name="Enabled Tool",
                enabled=True,
            ),
            Tool(
                tenant_id=TENANT,
                tool_id="wf_disabled_tool",
                name="Disabled Tool",
                enabled=False,
            ),
        ]
    )
    await session.flush()

    enabled_report = await validate_workflow(
        session, TENANT, _tool_dsl("wf_enabled_tool")
    )
    disabled_report = await validate_workflow(
        session, TENANT, _tool_dsl("wf_disabled_tool")
    )

    assert enabled_report.valid is True
    assert disabled_report.valid is False
    assert "tool_not_found_or_disabled" in _codes(disabled_report)
    issue = next(
        item
        for item in disabled_report.errors
        if item.code == "tool_not_found_or_disabled"
    )
    assert issue.node_id == "lookup"


async def test_cross_tenant_agent_is_invisible(session):
    session.add(
        Agent(
            tenant_id=OTHER_TENANT,
            name="wf-shared-agent",
            agent_type="device",
            status="active",
        )
    )
    await session.flush()

    report = await validate_workflow(session, TENANT, _agent_dsl("wf-shared-agent"))

    assert report.valid is False
    assert "agent_not_found_or_disabled" in _codes(report)


async def test_cross_tenant_tool_is_invisible(session):
    session.add(
        Tool(
            tenant_id=OTHER_TENANT,
            tool_id="wf_shared_tool",
            name="Shared Tool",
            enabled=True,
        )
    )
    await session.flush()

    report = await validate_workflow(session, TENANT, _tool_dsl("wf_shared_tool"))

    assert report.valid is False
    assert "tool_not_found_or_disabled" in _codes(report)


async def test_validate_workflow_returns_structural_errors_without_lookup(session):
    report = await validate_workflow(
        session,
        TENANT,
        {"nodes": [], "edges": []},
    )

    assert report.valid is False
    assert "nodes_empty" in _codes(report)
