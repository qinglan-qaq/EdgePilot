"""P6 演示链路：生成夹具、发布 Agent/工作流，并可选跑通「审批 → 报告」。

脚本只通过 HTTP API 操作业务数据，事件 id 从当前租户真实 ``vision_events``
读取，再写进 Mock LLM 夹具，保证报告里的证据引用可以点开对应事件。这样
换库/重置自增序列后重新执行即可，不把动态 id 固化进仓库。

用法（backend 目录）::

    SEED_PASSWORD='...' python scripts/seed_workflow_demo.py
    SEED_PASSWORD='...' python scripts/seed_workflow_demo.py --run

``--run`` 会以 operator 身份触发、等待审批、以 operator 审批，并校验运行
输出、节点状态与观测 trace。需要 API、Celery worker 与 Postgres 均已启动。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

BACKEND_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE_DIR = BACKEND_ROOT / "demo" / "fixtures" / "llm"
DEFAULT_BASE_URL = os.environ.get(
    "EDGEPILOT_API_BASE", "http://127.0.0.1:8000/api/v1"
)

VISION_AGENT = "demo-vision"
REPORT_AGENT = "demo-report"
WORKFLOW_NAME = "园区周界巡检（演示）"
WORKFLOW_DESCRIPTION = "P6 退出验收：视觉分析 → 条件 → 人工审批 → 报告 → trace"

TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "timeout", "cancelled"})


class DemoError(RuntimeError):
    """演示脚本的可预期失败；``main`` 只打印消息，不抛堆栈。"""


# ----------------------------------------------------------------- API 封装


class Api:
    """极简 HTTP 客户端：统一解包 ``{code, message, data}`` 信封。"""

    def __init__(self, client: httpx.AsyncClient, token: str | None = None) -> None:
        self._client = client
        self._token = token

    def with_token(self, token: str) -> "Api":
        return Api(self._client, token)

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        response = await self._client.request(
            method, path, headers=headers, **kwargs
        )
        if response.status_code >= 400:
            raise DemoError(
                f"{method} {path} -> {response.status_code}: {response.text[:500]}"
            )
        body = response.json()
        if body.get("code") not in (0, "0"):
            raise DemoError(f"{method} {path} 返回错误：{body}")
        return body.get("data")


async def _login(api: Api, email: str, password: str) -> str:
    data = await api.request(
        "POST", "/auth/login", json={"email": email, "password": password}
    )
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not token:
        raise DemoError(f"{email} 登录未返回 access_token")
    return token


# --------------------------------------------------------------- 夹具与 DSL


def _write_fixture(directory: Path, name: str, content: dict) -> Path:
    """写 Mock LLM 夹具；``content`` 会被序列化成 ``content`` 字符串。"""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "content": json.dumps(content, ensure_ascii=False),
        "finish_reason": "stop",
        # 不填 usage：Mock 没有真实 token 计量，按“不可用”处理，避免估算。
    }
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _build_fixtures(directory: Path, event: dict) -> tuple[Path, Path, dict]:
    event_id = int(event["id"])
    event_type = str(event.get("event_type") or "unknown")
    severity = str(event.get("severity") or "unknown")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    zone = str(payload.get("zone") or "未知区域")
    ref = str(event_id)
    snippet = f"事件 #{event_id}：{zone} / {event_type} / {severity}"

    vision = {
        "summary": f"{zone}检测到 {severity} 级 {event_type} 事件，需人工复核。",
        "events": [
            {
                "event_id": event_id,
                "event_type": event_type,
                "severity": severity,
                "zone": zone,
            }
        ],
        "evidence": [{"type": "event_id", "ref": ref, "snippet": snippet}],
    }
    markdown = "\n".join(
        [
            f"# {zone}巡检报告",
            "",
            "## 结论",
            "",
            f"- {zone}存在 1 条 {severity} 级 {event_type} 事件，建议现场复核。[1]",
            "",
            "## 证据",
            "",
            f"1. {snippet}",
            "",
            "> 数据来源：演示种子事件（simulated=true），非真实设备检测结论；",
            "> 原始事件未提供置信度，报告不做估算。",
            "",
        ]
    )
    report = {
        "title": f"{zone}巡检报告",
        "markdown": markdown,
        "highlights": [
            {
                "statement": f"{zone}存在 {severity} 级 {event_type} 事件，需现场复核",
                "evidence": [
                    {"type": "event_id", "ref": ref, "snippet": snippet}
                ],
            }
        ],
        "evidence": [{"type": "event_id", "ref": ref, "snippet": snippet}],
    }
    return (
        _write_fixture(directory, "demo_vision", vision),
        _write_fixture(directory, "demo_report", report),
        {"event_id": event_id, "zone": zone, "event_type": event_type},
    )


def _workflow_dsl() -> dict:
    """六节点演示 DSL；真/假分支各接一个 end，避免 end 多入边丢输出。"""
    return {
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "title": "接收巡检任务",
                "timeout_ms": 60000,
                "position": {"x": 40, "y": 160},
            },
            {
                "id": "vision",
                "type": "agent",
                "title": "视觉事件分析",
                "agent": VISION_AGENT,
                "timeout_ms": 60000,
                "position": {"x": 260, "y": 160},
            },
            {
                "id": "condition",
                "type": "condition",
                "title": "是否有可回溯结论",
                "expression": "$vision.output.summary != ''",
                "timeout_ms": 60000,
                "position": {"x": 500, "y": 160},
            },
            {
                "id": "approval",
                "type": "approval",
                "title": "报告发布审批",
                "approvers": ["operator"],
                "timeout_ms": 3600000,
                "position": {"x": 740, "y": 160},
            },
            {
                "id": "report",
                "type": "agent",
                "title": "生成巡检报告",
                "agent": REPORT_AGENT,
                "input_map": {"input": "$input", "vision": "$vision.output"},
                "timeout_ms": 60000,
                "position": {"x": 980, "y": 160},
            },
            {
                "id": "end_report",
                "type": "end",
                "title": "报告已生成",
                "timeout_ms": 60000,
                "position": {"x": 1220, "y": 160},
            },
            {
                "id": "end_insufficient",
                "type": "end",
                "title": "证据不足结束",
                "timeout_ms": 60000,
                "position": {"x": 740, "y": 360},
            },
        ],
        "edges": [
            {"from": "start", "to": "vision"},
            {"from": "vision", "to": "condition"},
            {"from": "condition", "to": "approval", "when": "true"},
            {"from": "condition", "to": "end_insufficient", "when": "false"},
            {"from": "approval", "to": "report"},
            {"from": "report", "to": "end_report"},
        ],
    }


# --------------------------------------------------------------- 数据准备


async def _find_by_name(api: Api, path: str, name: str) -> dict | None:
    page = await api.request("GET", path, params={"page": 1, "size": 100})
    for item in page.get("items", []):
        if item.get("name") == name:
            return item
    return None


async def _ensure_agent(
    admin: Api, name: str, agent_type: str, fixture: str
) -> tuple[dict, bool]:
    config = {"mock_fixture": fixture}
    existing = await _find_by_name(admin, "/agents", name)
    if existing is None:
        created = await admin.request(
            "POST",
            "/agents",
            json={
                "name": name,
                "agent_type": agent_type,
                "config": config,
                "status": "active",
            },
        )
        return created, True

    if existing.get("agent_type") != agent_type:
        raise DemoError(
            f"同名 Agent {name!r} 已存在但类型为 {existing.get('agent_type')!r}，"
            f"期望 {agent_type!r}；请先人工处理冲突"
        )
    if existing.get("config") == config and existing.get("status") == "active":
        return existing, False
    updated = await admin.request(
        "PATCH", f"/agents/{existing['id']}", json={"config": config, "status": "active"}
    )
    return updated, True


async def _ensure_workflow(admin: Api) -> tuple[dict, bool]:
    dsl = _workflow_dsl()
    existing = await _find_by_name(admin, "/workflows", WORKFLOW_NAME)
    if existing is None:
        created = await admin.request(
            "POST",
            "/workflows",
            json={
                "name": WORKFLOW_NAME,
                "description": WORKFLOW_DESCRIPTION,
                "dsl": dsl,
            },
        )
        return created, True

    # 列表项不含 dsl，必须取详情再比对，否则每次都会误判草稿已变更并重发布。
    detail = await admin.request("GET", f"/workflows/{existing['id']}")
    if detail.get("dsl") != dsl:
        detail = await admin.request(
            "PATCH",
            f"/workflows/{existing['id']}",
            json={"description": WORKFLOW_DESCRIPTION, "dsl": dsl},
        )
    versions = await admin.request(
        "GET", f"/workflows/{detail['id']}/versions"
    )
    latest = max(versions, key=lambda item: item["version"], default=None)
    if latest is None or latest.get("dsl") != dsl:
        await admin.request("POST", f"/workflows/{detail['id']}/publish")
        return detail, True
    return detail, False


# --------------------------------------------------------------- 运行验收


async def _wait_for(
    api: Api, run_id: int, predicate, *, timeout_s: int, label: str
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last: dict | None = None
    while asyncio.get_running_loop().time() < deadline:
        last = await api.request("GET", f"/runs/{run_id}")
        status = last.get("status")
        if predicate(last):
            return last
        if status in TERMINAL_RUN_STATUSES:
            raise DemoError(f"运行在等待{label}时提前结束：{status} / {last.get('error')}")
        await asyncio.sleep(1)
    raise DemoError(
        f"等待{label}超时（{timeout_s}s），最后状态：{last.get('status') if last else 'unknown'}"
    )


def _flatten_tree(nodes: list[dict]) -> list[dict]:
    flat: list[dict] = []
    for node in nodes:
        flat.append(node)
        children = node.get("children")
        if isinstance(children, list):
            flat.extend(_flatten_tree(children))
    return flat


async def _verify_trace(admin: Api, run: dict) -> dict:
    trace_id = run.get("trace_id")
    if not isinstance(trace_id, str) or len(trace_id) != 32:
        raise DemoError(f"运行 trace_id 非法：{trace_id!r}")
    tree = await admin.request("GET", f"/observability/traces/{trace_id}/tree")
    flat = _flatten_tree(tree)
    kinds: dict[str, int] = {}
    for span in flat:
        kind = str(span.get("kind") or span.get("span_type") or "unknown")
        kinds[kind] = kinds.get(kind, 0) + 1
    if not any(kind in {"workflow", "workflow_run", "chain"} for kind in kinds):
        raise DemoError(f"trace 树缺少 workflow span：{kinds}")
    if kinds.get("agent", 0) < 2:
        raise DemoError(f"trace 树 agent span 少于 2 个：{kinds}")
    return {"trace_id": trace_id, "span_count": len(flat), "kinds": kinds}


async def _run_chain(operator: Api, admin: Api, workflow_id: int) -> dict:
    created = await operator.request(
        "POST",
        f"/workflows/{workflow_id}/runs",
        json={
            "input": {"focus": "东门大厅", "note": "P6 演示运行"},
            "trigger_type": "manual",
        },
    )
    run_id = int(created["id"])
    waiting = await _wait_for(
        operator,
        run_id,
        lambda detail: detail.get("status") == "waiting_approval",
        timeout_s=120,
        label="审批",
    )
    approval_node = next(
        node for node in waiting["nodes"] if node["node_type"] == "approval"
    )
    if not approval_node.get("approval"):
        raise DemoError("审批节点缺少 approval 元数据")
    approval = approval_node["approval"]
    if "operator" not in approval.get("approvers", []):
        raise DemoError(f"审批人配置未包含 operator：{approval}")

    await operator.request(
        "POST",
        f"/runs/{run_id}/approve",
        json={"comment": "演示审批通过：报告可发布"},
    )
    final = await _wait_for(
        operator,
        run_id,
        lambda detail: detail.get("status") in TERMINAL_RUN_STATUSES,
        timeout_s=120,
        label="运行结束",
    )
    if final.get("status") != "succeeded":
        raise DemoError(
            f"运行未成功：{final.get('status')} / {final.get('error')}"
        )
    output = final.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("markdown"), str):
        raise DemoError(f"运行输出缺少 markdown：{output!r}")
    statuses = {node["node_id"]: node["status"] for node in final["nodes"]}
    if statuses.get("report") != "succeeded" or statuses.get("end_report") != "succeeded":
        raise DemoError(f"报告分支节点状态异常：{statuses}")
    if statuses.get("end_insufficient") != "skipped":
        raise DemoError(f"未走到的分支应 skipped：{statuses}")
    trace = await _verify_trace(admin, final)
    return {
        "run_id": run_id,
        "status": final["status"],
        "node_statuses": statuses,
        "trace": trace,
        "output_markdown": output["markdown"],
        "highlight_count": len(output.get("highlights") or []),
    }


# --------------------------------------------------------------------- main


async def _main(args: argparse.Namespace) -> dict:
    password = os.environ.get("SEED_PASSWORD", "")
    if len(password) < 8:
        raise DemoError("请通过 SEED_PASSWORD 提供至少 8 位演示账号密码")

    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
        anonymous = Api(client)
        admin = anonymous.with_token(
            await _login(anonymous, "admin@edgepilot.local", password)
        )
        operator = anonymous.with_token(
            await _login(anonymous, "operator@edgepilot.local", password)
        )

        if args.event_id is not None:
            event = await admin.request("GET", f"/events/{args.event_id}")
        else:
            page = await admin.request(
                "GET",
                "/events",
                params={"event_type": "dwell", "severity": "high", "size": 1},
            )
            items = page.get("items") or []
            if not items:
                raise DemoError(
                    "库里没有高严重度滞留事件；请先运行 scripts/seed_demo.py"
                )
            event = items[0]

        vision_fixture, report_fixture, event_meta = _build_fixtures(
            args.fixture_dir, event
        )
        print(
            "夹具已生成："
            f"{vision_fixture.name}, {report_fixture.name}（事件 #{event_meta['event_id']}）"
        )

        vision, vision_changed = await _ensure_agent(
            admin, VISION_AGENT, "vision", "demo_vision"
        )
        report, report_changed = await _ensure_agent(
            admin, REPORT_AGENT, "report", "demo_report"
        )
        for agent, changed, label in (
            (vision, vision_changed, "vision"),
            (report, report_changed, "report"),
        ):
            if changed or int(agent.get("current_version") or 0) < 1:
                await admin.request("POST", f"/agents/{agent['id']}/publish")
                print(f"Agent {label} 已发布：{agent['name']}#{agent['id']}")

        workflow, published = await _ensure_workflow(admin)
        workflow_id = int(workflow["id"])
        print(
            f"工作流已就绪：{workflow['name']}#{workflow_id}"
            f"（{'已发布新版本' if published else '复用当前版本'}）"
        )

        result: dict[str, Any] = {
            "workflow_id": workflow_id,
            "vision_agent_id": vision["id"],
            "report_agent_id": report["id"],
            "event": event_meta,
        }
        if args.run:
            result["run"] = await _run_chain(operator, admin, workflow_id)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description="准备并验收 P6 演示链路")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--fixture-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--event-id", type=int, default=None)
    parser.add_argument("--run", action="store_true", help="触发并审批一次运行")
    args = parser.parse_args()
    try:
        result = asyncio.run(_main(args))
    except (DemoError, httpx.HTTPError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
