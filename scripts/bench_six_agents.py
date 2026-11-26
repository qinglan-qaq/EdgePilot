"""6 Agent 协作延迟基准（P7 Task 28）。

一次请求 = Supervisor planner + 五个领域 Agent（device/vision/diagnosis/
operations/report），全部命中 Mock LLM 夹具。默认只测 Mock；真实 LLM 需要
API Key 与网络，不得用 Mock 数字冒充真实延迟。

用法（需要 API、Postgres、Celery worker 已启动）::

    export EDGEPILOT_TOKEN='<admin access token>'
    python scripts/bench_six_agents.py --runs 20

Token 可临时在 api 容器内生成（15 分钟有效）::

    docker compose -f docker/compose/docker-compose.yml --profile app exec api \
      python -c "from app.core.security import create_token; print(create_token('1','00000000-0000-0000-0000-000000000001','admin','access'))"

夹具写入 ``backend/demo/fixtures/llm/``，与 compose 的只读挂载目录一致；
容器内 API 能直接读到新文件，不需要重建镜像。脚本默认保留创建的 6 个
Agent 便于复测，``--cleanup`` 会在结束时删除它们。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "backend" / "demo" / "fixtures" / "llm"
DEFAULT_BASE_URL = os.environ.get(
    "EDGEPILOT_API_BASE", "http://127.0.0.1:8000/api/v1"
)

DOMAIN_ROLES: tuple[str, ...] = ("device", "vision", "diagnosis", "operations", "report")

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

ROLE_OUTPUTS: dict[str, dict[str, Any]] = {
    "device": {
        "answer": "设备在线",
        "device_status": "online",
        "evidence": [
            {"type": "event_id", "ref": "bench-device-1", "snippet": "heartbeat ok"}
        ],
    },
    "vision": {
        "summary": "画面中未发现人员违规闯入",
        "events": [],
        "evidence": [
            {"type": "event_id", "ref": "bench-vision-1", "snippet": "frame clean"}
        ],
    },
    "diagnosis": {
        "conclusion": "设备过热",
        "possible_causes": ["风扇转速不足"],
        "evidence": [
            {"type": "event_id", "ref": "bench-diag-1", "snippet": "temperature high"}
        ],
    },
    "operations": {
        "summary": "CPU 使用率偏高但服务可用",
        "findings": [],
        "evidence": [
            {"type": "metric", "ref": "cpu_usage", "snippet": "cpu high"}
        ],
    },
    "report": {
        "title": "现场设备检查报告",
        "markdown": (
            "# 现场设备检查报告\n\n"
            "## 结论\n\n"
            "1. [1] 设备在线\n"
            "2. [2] 画面中未发现人员违规闯入\n"
            "3. [3] 设备过热\n"
            "4. [4] CPU 使用率偏高但服务可用\n\n"
            "## 证据\n\n"
            "- [1] `event_id:bench-device-1` — heartbeat ok\n"
            "- [2] `event_id:bench-vision-1` — frame clean\n"
            "- [3] `event_id:bench-diag-1` — temperature high\n"
            "- [4] `metric:cpu_usage` — cpu high\n"
        ),
        "highlights": [
            {
                "statement": "设备在线",
                "evidence": [{"type": "event_id", "ref": "bench-device-1"}],
            }
        ],
        "evidence": [
            {"type": "event_id", "ref": "bench-device-1"},
            {"type": "event_id", "ref": "bench-vision-1"},
            {"type": "event_id", "ref": "bench-diag-1"},
            {"type": "metric", "ref": "cpu_usage"},
        ],
    },
}


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


def _write_fixture(name: str, content: dict[str, Any]) -> None:
    """写 Mock 响应；不填 usage，Mock 没有真实 token 计量。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"content": json.dumps(content, ensure_ascii=False), "finish_reason": "stop"}
    (FIXTURE_DIR / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


class Api:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"
        response = await self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        body = response.json()
        if body.get("code") not in (0, "0"):
            raise RuntimeError(f"{method} {path} 返回错误：{body}")
        return body.get("data")


async def _create_agents(api: Api, tag: str) -> dict[str, int]:
    ids: dict[str, int] = {}
    specs = [("supervisor", "supervisor", "supervisor_plan")] + [
        (role, role, f"{role}_ok") for role in DOMAIN_ROLES
    ]
    for role, agent_type, fixture in specs:
        fixture_name = f"bench6_{tag}_{fixture}"
        _write_fixture(fixture_name, SUPERVISOR_PLAN if role == "supervisor" else ROLE_OUTPUTS[role])
        created = await api.request(
            "POST",
            "/agents",
            json={
                "name": f"bench6-{tag}-{role}",
                "agent_type": agent_type,
                "config": {
                    "system_prompt": f"你是 {role} 角色助手",
                    "mock_fixture": fixture_name,
                },
            },
        )
        await api.request("POST", f"/agents/{created['id']}/publish")
        ids[role] = int(created["id"])
    return ids


async def _run(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("EDGEPILOT_TOKEN", "")
    if not token:
        raise SystemExit("缺少 admin token：传 --token 或设置 EDGEPILOT_TOKEN")

    tag = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=args.base_url, timeout=120.0) as client:
        api = Api(client, token)
        ids = await _create_agents(api, tag)
        latencies: list[float] = []
        statuses: list[str] = []
        for index in range(args.runs):
            started = time.perf_counter()
            data = await api.request(
                "POST",
                f"/agents/{ids['supervisor']}/test",
                json={"task": {"question": "现场设备是否正常"}},
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            statuses.append(str(data.get("status")))
            assignments = (data.get("output") or {}).get("assignments") or []
            if data.get("status") != "succeeded" or len(assignments) != 5:
                raise RuntimeError(
                    f"第 {index + 1} 次运行未成功：status={data.get('status')} "
                    f"assignments={len(assignments)} error={data.get('error')}"
                )
            latencies.append(elapsed_ms)

        result = {
            "mode": "mock",
            "runs": len(latencies),
            "agent_count": 6,
            "p50_ms": round(statistics.median(latencies), 3),
            "p95_ms": round(_percentile(latencies, 0.95), 3),
            "p99_ms": round(_percentile(latencies, 0.99), 3),
            "min_ms": round(min(latencies), 3),
            "max_ms": round(max(latencies), 3),
            "statuses": sorted(set(statuses)),
            "method": "HTTP POST /agents/{supervisor_id}/test -> 完整响应",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))

        if args.cleanup:
            # 已产生运行记录的 Agent 不允许硬删除（409）；置为 disabled 并清理
            # 夹具即可，运行历史保留用于审计。
            for agent_id in ids.values():
                await api.request("PATCH", f"/agents/{agent_id}", json={"status": "disabled"})
            for name in (
                f"bench6_{tag}_supervisor_plan",
                *(f"bench6_{tag}_{role}_ok" for role in DOMAIN_ROLES),
            ):
                (FIXTURE_DIR / f"{name}.json").unlink(missing_ok=True)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="6 Agent 协作延迟基准（Mock LLM）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--token", default="")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--cleanup", action="store_true", help="结束后删除基准 Agent 与夹具")
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs 必须为正整数")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
