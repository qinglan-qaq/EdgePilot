"""P3 退出验收：一次 HTTP 请求 → 6 个 Agent 同 trace 端到端。

验收契约（平台计划 P3 退出 / MVP 设计 §14 P3）：

* ``POST /api/v1/agents/{supervisor_id}/test`` 触发 Supervisor 编排；
* ``agent_runs`` 中该 ``trace_id`` 恰好 **6 行**（Supervisor + 五个领域 Agent）；
* ``GET /observability/traces/{id}/tree`` 里恰好 **6 个** ``span_type="agent"``
  的 span（``supervisor.run`` + ``agent.<type>`` × 5），全部同 trace，
  子 span 的父节点都是 ``supervisor.run``，树根是 ``workflow``；
* 报告里的每条结论都能回溯到 ``evidence``（``[n]`` 恒在 ``1..len(evidence)``）。

测试全程使用确定性 mock fixture：每个角色一个 fixture（Supervisor 的
planner 与五个子 Agent 各自独立），不触网、不调用真实 LLM。观测写入器
显式安装 ``BatchWriter``（ASGITransport 不跑 lifespan，collector 不会
自动启动），请求结束后 ``collector.stop()`` 排空并落库。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.agents.report import EVIDENCE_TYPES, build_report
from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability import get_tracer, set_writer
from app.observability.writer import BatchWriter
from tests.conftest import TENANT, _make_published_agent, auth_headers

pytestmark = pytest.mark.e2e

#: Supervisor 派发的五个领域角色（顺序即 fixture 中的派发顺序）。
DOMAIN_ROLES: tuple[str, ...] = ("device", "vision", "diagnosis", "operations", "report")

TASK = {"question": "现场设备是否正常"}

# ---------------------------------------------------------------- mock fixtures

#: Supervisor planner 输出：plan + 5 条 assignments（每类角色一条）。
SUPERVISOR_PLAN = {
    "plan": [
        "检查设备在线状态",
        "检查现场画面",
        "诊断告警原因",
        "检查运维指标",
        "汇总生成报告",
    ],
    "assignments": [
        {
            "agent_type": "device",
            "goal": "检查设备是否在线",
            "input": {"question": "现场设备是否正常"},
        },
        {
            "agent_type": "vision",
            "goal": "检查现场画面是否有异常",
            "input": {"camera": "cam-1"},
        },
        {
            "agent_type": "diagnosis",
            "goal": "诊断温度告警原因",
            "input": {"question": "机柜温度告警原因"},
        },
        {
            "agent_type": "operations",
            "goal": "检查服务与资源指标",
            "input": {"service": "edge-gateway"},
        },
        {
            "agent_type": "report",
            "goal": "汇总以上结论生成报告",
            "input": {"format": "markdown"},
        },
    ],
    "summary": "拆解为 5 个并行子任务。",
}

DEVICE_OUTPUT = {
    "answer": "设备在线",
    "device_status": "online",
    "evidence": [
        {"type": "event_id", "ref": "evt-device-1", "snippet": "heartbeat ok"},
    ],
}

VISION_OUTPUT = {
    "summary": "画面中未发现人员违规闯入",
    "events": [],
    "evidence": [
        {"type": "event_id", "ref": "evt-vision-1", "snippet": "frame 42 clean"},
    ],
}

DIAGNOSIS_OUTPUT = {
    "conclusion": "设备过热",
    "possible_causes": ["风扇转速不足"],
    "evidence": [
        {"type": "event_id", "ref": "evt-diag-1", "snippet": "temperature 82C"},
    ],
}

OPERATIONS_OUTPUT = {
    "summary": "CPU 使用率偏高但服务可用",
    "findings": [],
    "evidence": [
        {"type": "metric", "ref": "cpu_usage", "snippet": "cpu 78%"},
    ],
}

REPORT_OUTPUT = {
    "title": "现场设备检查报告",
    "markdown": (
        "# 现场设备检查报告\n\n"
        "## 结论\n\n"
        "1. [1] 设备在线\n"
        "2. [2] 画面中未发现人员违规闯入\n"
        "3. [3] 设备过热\n"
        "4. [4] CPU 使用率偏高但服务可用\n\n"
        "## 证据\n\n"
        "- [1] `event_id:evt-device-1` — heartbeat ok\n"
        "- [2] `event_id:evt-vision-1` — frame 42 clean\n"
        "- [3] `event_id:evt-diag-1` — temperature 82C\n"
        "- [4] `metric:cpu_usage` — cpu 78%\n"
    ),
    "highlights": [
        {
            "statement": "设备在线",
            "evidence": [{"type": "event_id", "ref": "evt-device-1"}],
        },
        {
            "statement": "设备过热",
            "evidence": [{"type": "event_id", "ref": "evt-diag-1"}],
        },
    ],
    "evidence": [
        {"type": "event_id", "ref": "evt-device-1"},
        {"type": "event_id", "ref": "evt-vision-1"},
        {"type": "event_id", "ref": "evt-diag-1"},
        {"type": "metric", "ref": "cpu_usage"},
    ],
}

#: fixture 名 → 该角色返回的结构化输出（用于断言「没有编造」）。
ROLE_OUTPUTS = {
    "device": DEVICE_OUTPUT,
    "vision": VISION_OUTPUT,
    "diagnosis": DIAGNOSIS_OUTPUT,
    "operations": OPERATIONS_OUTPUT,
    "report": REPORT_OUTPUT,
}


def _write_fixture(path: Path, content: dict, *, total_tokens: int = 20) -> None:
    """写一个确定性 mock 响应：content 为结构化 JSON，usage 回报真实 token。"""
    path.write_text(
        json.dumps(
            {
                "content": json.dumps(content, ensure_ascii=False),
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": total_tokens - 8,
                    "completion_tokens": 8,
                    "total_tokens": total_tokens,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def e2e_obs_and_llm(tmp_path, monkeypatch):
    """观测全采样 + mock LLM；每个角色一个独立 fixture 目录。"""
    import app.observability as obs

    fixture_dir = tmp_path / "llm"
    fixture_dir.mkdir()
    _write_fixture(fixture_dir / "supervisor_plan.json", SUPERVISOR_PLAN)
    for role in DOMAIN_ROLES:
        _write_fixture(fixture_dir / f"{role}_ok.json", ROLE_OUTPUTS[role])

    monkeypatch.setenv("OBS_ENABLED", "true")
    monkeypatch.setenv("OBS_SAMPLE_RATE", "1.0")
    monkeypatch.setenv("OBS_CAPTURE_MODE", "REDACTED")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("LLM_FIXTURE_DIR", str(fixture_dir))
    get_settings.cache_clear()
    obs.reset_tracer()
    try:
        yield fixture_dir
    finally:
        obs.reset_tracer()
        get_settings.cache_clear()


# ---------------------------------------------------------------------- helpers


def _flatten(nodes: list[dict]) -> list[dict]:
    """把 trace 树展平成 span 列表（先序）。"""
    flat: list[dict] = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten(node.get("children") or []))
    return flat


async def _seed_agents(client, admin_token) -> dict[str, int]:
    """六个角色各建一个已发布 Agent，返回 role → agent_id。"""
    ids = {
        "supervisor": await _make_published_agent(
            client, admin_token, "e2e-supervisor", "supervisor_plan",
            agent_type="supervisor",
        )
    }
    for role in DOMAIN_ROLES:
        ids[role] = await _make_published_agent(
            client, admin_token, f"e2e-{role}", f"{role}_ok", agent_type=role
        )
    return ids


async def _agent_run_rows(session, trace_id: str) -> list[dict]:
    rows = await session.execute(
        text("SELECT agent_id, agent_version, status FROM agent_runs WHERE trace_id = :t"),
        {"t": trace_id},
    )
    return [dict(row._mapping) for row in rows]


# ------------------------------------------------------------------------ 验收


async def test_supervisor_request_has_six_agent_runs_in_one_trace(
    client, admin_token, session, session_factory
):
    """一次 supervisor 试跑：6 行 agent_runs + 6 个 agent span + 可回溯报告。"""
    agent_ids = await _seed_agents(client, admin_token)
    # ASGITransport 不跑 lifespan，collector 不会自动启动：显式装写入器，
    # 请求结束后再 stop() 排空队列，确保查询前记录已落库。
    set_writer(BatchWriter(session_factory))

    token = set_tenant_id(TENANT)
    try:
        response = await client.post(
            f"/api/v1/agents/{agent_ids['supervisor']}/test",
            json={"task": TASK},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200
        data = response.json()["data"]

        # 1) 响应契约：Supervisor 试跑返回自己的 agent_run + 5 个子结果。
        assert data["status"] == "succeeded", (
            data.get("error"),
            [
                (i["agent_type"], i["status"], i.get("error"))
                for i in data["output"]["assignments"]
            ],
        )
        assert data["agent_id"] == agent_ids["supervisor"]
        assert data["agent_version"] >= 1
        assert data["id"] is not None
        assert re.fullmatch(r"[0-9a-f]{32}", data["trace_id"])
        assert re.fullmatch(r"[0-9a-f]{16}", data["span_id"])
        assert data["total_cost"] is None
        assert data["retry_count"] == 0
        assert data["total_tokens"] == 5 * 20

        output = data["output"]
        assert isinstance(output, dict)
        assignments = output["assignments"]
        assert len(assignments) == 5
        assert [item["agent_type"] for item in assignments] == list(DOMAIN_ROLES)
        assert all(item["status"] == "succeeded" for item in assignments)
        assert all(item["agent_run_id"] is not None for item in assignments)
        assert output["plan"] == [item["goal"] for item in assignments]
        assert "成功 5，失败 0" in output["summary"]

        # 相对「只来自 fixture」的可回溯证据集合：四个领域 Agent + report
        # 自身引用的同一批证据，逐条都能在 fixture 里找到。
        fixture_refs = {
            item["ref"]
            for payload in ROLE_OUTPUTS.values()
            for item in payload["evidence"]
        }
        assert output["evidence"]
        assert {item["ref"] for item in output["evidence"]} == fixture_refs

        trace_id = data["trace_id"]

        # 2) agent_runs：同 trace 恰好 6 行，全部 succeeded，每角色一行。
        await get_tracer().collector.stop()

        rows = await _agent_run_rows(session, trace_id)
        assert len(rows) == 6, rows
        assert {row["status"] for row in rows} == {"succeeded"}
        per_agent: dict[int, int] = {}
        for row in rows:
            per_agent[row["agent_id"]] = per_agent.get(row["agent_id"], 0) + 1
        assert per_agent == {agent_id: 1 for agent_id in agent_ids.values()}
        assert all(row["agent_version"] >= 1 for row in rows)

        # 3) span 树：根为 workflow，6 个 agent span 同一 trace。
        tree_resp = await client.get(
            f"/api/v1/observability/traces/{trace_id}/tree",
            headers=auth_headers(admin_token),
        )
        assert tree_resp.status_code == 200
        tree = tree_resp.json()["data"]
        assert len(tree) == 1, tree
        assert tree[0]["span_type"] == "workflow"

        flat = _flatten(tree)
        agent_spans = [span for span in flat if span["span_type"] == "agent"]
        assert len(agent_spans) == 6, [span["name"] for span in flat]
        names = sorted(span["name"] for span in agent_spans)
        assert names == sorted(["supervisor.run"] + [f"agent.{r}" for r in DOMAIN_ROLES])
        assert {span["trace_id"] for span in flat} == {trace_id}
        assert all(span["status"] == "succeeded" for span in agent_spans)

        supervisor_span = next(
            span for span in agent_spans if span["name"] == "supervisor.run"
        )
        child_spans = [span for span in agent_spans if span["name"] != "supervisor.run"]
        assert supervisor_span["parent_span_id"] == tree[0]["span_id"]
        assert {span["parent_span_id"] for span in child_spans} == {
            supervisor_span["span_id"]
        }

        # 4) 报告可回溯：所有 [n] 编号都落在 evidence 内，类型白名单内。
        report = build_report(assignments)
        citations = [int(n) for n in re.findall(r"\[(\d+)\]", report["markdown"])]
        assert citations, report["markdown"]
        assert all(1 <= number <= len(report["evidence"]) for number in citations)
        assert report["evidence"], "报告必须至少有一条可回溯证据"
        assert all(
            item["type"] in EVIDENCE_TYPES for item in report["evidence"]
        )
        assert all(item["evidence"] for item in report["highlights"])
        assert report["finding_count"] == 4  # device/vision/diagnosis/operations

        # 5) 结论只来自 fixture：每条结论与证据都是预置内容，未被编造。
        by_type = {item["agent_type"]: item for item in assignments}
        assert by_type["device"]["output"]["answer"] == DEVICE_OUTPUT["answer"]
        assert by_type["vision"]["output"]["summary"] == VISION_OUTPUT["summary"]
        assert by_type["diagnosis"]["output"]["conclusion"] == DIAGNOSIS_OUTPUT["conclusion"]
        assert by_type["operations"]["output"]["summary"] == OPERATIONS_OUTPUT["summary"]
        assert by_type["report"]["output"]["markdown"] == REPORT_OUTPUT["markdown"]
        assert {item["ref"] for item in report["evidence"]} <= fixture_refs
    finally:
        reset_tenant_id(token)
