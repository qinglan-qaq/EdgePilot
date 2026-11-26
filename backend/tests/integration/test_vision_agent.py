"""Task 18：Vision Agent 研判集成测试（TDD red 阶段）。

需求来源：``.superpowers/sdd/2026-11-26-edgepilot-vision-plan/task-18-brief.md``。

覆盖的契约：

* 每任务默认只研判一次：第二次 analyze 直接复用既有 ``agent_runs`` 行；
* LLM 输入只包含任务摘要 + 单条事件 + 模型/视频元数据，绝不逐帧喂 frames；
* 结果行记录 ``agent_event_id`` / ``agent_run_id`` / ``agent_trace_id``；
* Agent 失败不改 ``vision_tasks.status``，且失败后允许受控重试、不产生重复成功 run；
* stale ``running`` 超过 ``vision_task_timeout_s`` 后恢复为 failed(agent_interrupted) 并重试，
  未超时仍返回 409 ``vision_agent_running``；
* ``/vision/tasks/{uid}/analyze`` 与 ``/vision/events/{event_id}/analyze`` 走同一套
  “每任务成功一次”的复用规则，后者使用指定事件；
* 跨租户 404；研判权限 admin + operator，viewer 403；
* 既有 ``POST /agents/{id}/test`` 仍仅 admin 可用。

被测服务 ``app.services.vision_agent_service`` 尚未实现，当前失败属预期。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from app.agents.llm import LLMResponse
from app.core.config import get_settings
from app.core.exceptions import Conflict
from app.core.security import hash_password
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.agent import Agent
from app.models.event import VisionEvent
from app.models.run import AgentRun
from app.models.user import User
from app.models.vision import VisionResult, VisionTask
from tests.conftest import PASSWORD, TENANT, auth_headers, login_token

ADMIN_EMAIL = "vision-agent-admin@edgepilot.local"
OPERATOR_EMAIL = "vision-agent-operator@edgepilot.local"
SUMMARY_MARKER = "任务摘要-人员进入警戒区"
OLD_EVENT_MARKER = "specified-old-event"
NEW_EVENT_MARKER = "latest-new-event"

# 与 /agents/{id}/test 完全一致的响应字段（task-18 简报）。
RESPONSE_FIELDS = {
    "id",
    "agent_id",
    "agent_version",
    "status",
    "output",
    "error",
    "trace_id",
    "span_id",
    "total_tokens",
    "total_cost",
    "retry_count",
}


@pytest.fixture(autouse=True)
def deterministic_obs_and_tenant(monkeypatch):
    """开启观测（trace_id 可断言）并建立服务层直调所需的租户上下文。"""
    import app.observability as obs

    monkeypatch.setenv("OBS_ENABLED", "true")
    get_settings.cache_clear()
    obs.reset_tracer()

    token = set_tenant_id(TENANT)
    try:
        yield
    finally:
        reset_tenant_id(token)
        obs.reset_tracer()
        get_settings.cache_clear()


@pytest.fixture
def vision_service_cls():
    """延迟导入：服务未实现时让用例逐个报 ModuleNotFoundError，而非整文件收集失败。"""
    from app.services.vision_agent_service import VisionAgentService

    return VisionAgentService


class FakeLLM:
    """最小 LLM 替身：记录 messages，可切换为失败，绝不触网。"""

    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[list[dict]] = []
        self.failures_remaining = 0
        self.last_task: list[dict] | None = None
        self.ok_payload = {
            "summary": "检测到人员进入警戒区，建议现场核实。",
            "events": [{"event_type": "vision.detection", "severity": "medium"}],
            "evidence": [
                {"type": "event", "ref": "vision_event", "snippet": "person bbox"}
            ],
        }

    async def chat(
        self,
        messages,
        *,
        response_schema=None,
        temperature=0.0,
        tools=None,
        model_config=None,
    ) -> LLMResponse:
        self.calls += 1
        self.messages.append(messages)
        self.last_task = messages
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            return LLMResponse(
                content="",
                error_code="provider_error",
                provider="mock",
                model_name="mock",
            )
        return LLMResponse(
            content=json.dumps(self.ok_payload, ensure_ascii=False),
            usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            finish_reason="stop",
            provider="mock",
            model_name="mock",
        )


@pytest.fixture
def fake_llm(monkeypatch, vision_service_cls):
    fake = FakeLLM()
    # 服务按简报通过 ``self._llm(agent)`` 取客户端；用 class 级替身覆盖，
    # 无论实现把它写成实例方法还是 staticmethod 都能注入。
    monkeypatch.setattr(
        vision_service_cls, "_llm", lambda *args, **kwargs: fake, raising=False
    )
    return fake


@pytest.fixture
def failing_llm(monkeypatch, vision_service_cls):
    fake = FakeLLM()
    fake.failures_remaining = 10**6
    monkeypatch.setattr(
        vision_service_cls, "_llm", lambda *args, **kwargs: fake, raising=False
    )
    return fake


@pytest.fixture
async def admin_user(session):
    user = User(
        tenant_id=TENANT,
        email=ADMIN_EMAIL,
        password_hash=hash_password(PASSWORD),
        display_name="Vision Admin",
        role="admin",
        status="active",
    )
    session.add(user)
    await session.flush()
    return user


@pytest.fixture
async def operator_token(client, session):
    user = User(
        tenant_id=TENANT,
        email=OPERATOR_EMAIL,
        password_hash=hash_password(PASSWORD),
        display_name="Vision Operator",
        role="operator",
        status="active",
    )
    session.add(user)
    await session.flush()
    return await login_token(client, OPERATOR_EMAIL)


@pytest.fixture
async def vision_agent(session):
    agent = Agent(
        tenant_id=TENANT,
        name=f"vision-agent-{uuid.uuid4().hex[:8]}",
        agent_type="vision",
        status="active",
        current_version=1,
        config={"system_prompt": "你是视觉研判助手"},
    )
    session.add(agent)
    await session.flush()
    return agent


@dataclass
class SeededTask:
    task: VisionTask
    result: VisionResult
    events: list[VisionEvent]


async def _seed_completed_task(
    session, *, with_events: bool = True, summary: str = SUMMARY_MARKER
) -> SeededTask:
    """直接落一条 completed 任务 + 结果汇总 + 两条事件。

    服务层测试不关心上传/推理链路，只需要真实 PG 行；外层事务统一回滚。
    """
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    task = VisionTask(
        tenant_id=TENANT,
        task_uid=uid,
        dedup_key=f"vision:{uid}",
        status="completed",
        source_type="upload",
        video_filename="gate.mp4",
        video_path=f"videos/{TENANT}/{uid}.mp4",
        video_sha256="a" * 64,
        file_size_bytes=1024,
        conf_threshold=0.25,
        frame_stride=1,
        target_samples=900,
        progress=100,
        processed_frames=100,
        detection_count=2,
        frame_count=100,
        sampled_every_n_frames=1,
        model_name="yolo11n",
        model_version="v1",
        device="cpu",
        video_fps=25,
        video_duration_ms=4000,
        video_width=640,
        video_height=360,
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=30),
        created_at=now - timedelta(hours=1),
    )
    session.add(task)
    await session.flush()

    result = VisionResult(
        tenant_id=TENANT,
        task_id=task.id,
        task_uid=uid,
        artifact_path=f"jobs/{uid}/result.json",
        artifact_bytes=256,
        result_sha256="d" * 64,
        result_schema_version="vision-result/v1",
        frame_count=100,
        detection_count=2,
        class_counts={"person": 2},
        peak_confidence=0.91,
        event_count=2 if with_events else 0,
        summary={"summary": summary, "person_count": 2, "model": "yolo11n"},
        agent_status="none",
    )
    session.add(result)
    await session.flush()

    events: list[VisionEvent] = []
    if with_events:
        events = [
            VisionEvent(
                tenant_id=TENANT,
                event_type="vision.detection",
                severity="medium",
                occurred_at=now - timedelta(seconds=10),
                confidence=0.82,
                bbox={"x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0},
                model_version="v1",
                payload={"task_uid": uid, "marker": OLD_EVENT_MARKER},
                dedup_key=f"vision:{uid}:old",
                status="new",
            ),
            VisionEvent(
                tenant_id=TENANT,
                event_type="security.zone_intrusion",
                severity="high",
                occurred_at=now - timedelta(seconds=1),
                confidence=0.93,
                bbox={"x": 5.0, "y": 6.0, "w": 7.0, "h": 8.0},
                model_version="v1",
                payload={"task_uid": uid, "marker": NEW_EVENT_MARKER},
                dedup_key=f"vision:{uid}:new",
                status="new",
            ),
        ]
        session.add_all(events)
        await session.flush()
    return SeededTask(task=task, result=result, events=events)


@pytest.fixture
async def completed_task_with_events(session):
    return await _seed_completed_task(session)


@pytest.fixture
async def completed_task_without_events(session):
    return await _seed_completed_task(session, with_events=False)


async def _succeeded_run_count(session, agent_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.agent_id == agent_id, AgentRun.status == "succeeded")
        )
        or 0
    )


# --------------------------------------------------------------------- 服务层


@pytest.mark.asyncio
async def test_analyze_uses_summary_not_frames_and_records_links(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    fake_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    response = await vision_service_cls(session).analyze_task(
        seeded.task.task_uid, user=admin_user
    )

    assert response["status"] == "succeeded"
    assert set(response) == RESPONSE_FIELDS
    assert response["agent_id"] == vision_agent.id
    assert response["total_tokens"] == 20
    assert response["trace_id"] and len(response["trace_id"]) == 32
    assert response["span_id"] and len(response["span_id"]) == 16

    # 输入只含任务摘要 + 单条事件 + 模型/视频元数据，不能出现整段 frames。
    prompt = json.dumps(fake_llm.last_task, ensure_ascii=False, default=str)
    assert seeded.task.task_uid in prompt
    assert SUMMARY_MARKER in prompt
    assert NEW_EVENT_MARKER in prompt  # 默认取最新事件
    assert "frames" not in prompt
    assert OLD_EVENT_MARKER not in prompt

    await session.refresh(seeded.result)
    assert seeded.result.agent_status == "succeeded"
    assert seeded.result.agent_run_id == response["id"]
    assert seeded.result.agent_trace_id == response["trace_id"]
    assert seeded.result.agent_event_id == seeded.events[1].id
    assert seeded.result.agent_error is None

    run = await session.get(AgentRun, response["id"])
    assert run is not None and run.status == "succeeded"
    assert run.agent_id == vision_agent.id
    assert run.trace_id == response["trace_id"]
    assert run.input["task"]["task_uid"] == seeded.task.task_uid
    assert run.input["task"]["summary"]["summary"] == SUMMARY_MARKER
    assert run.input["event"]["event_type"] == seeded.events[1].event_type
    assert "frames" not in json.dumps(run.input, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_second_analyze_reuses_existing_run(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    fake_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    service = vision_service_cls(session)

    first = await service.analyze_task(seeded.task.task_uid, user=admin_user)
    second = await service.analyze_task(seeded.task.task_uid, user=admin_user)

    assert first["status"] == "succeeded"
    assert second["id"] == first["id"]
    assert second["trace_id"] == first["trace_id"]
    assert fake_llm.calls == 1
    assert await _succeeded_run_count(session, vision_agent.id) == 1


@pytest.mark.asyncio
async def test_agent_failure_does_not_change_task_status(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    failing_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    response = await vision_service_cls(session).analyze_task(
        seeded.task.task_uid, user=admin_user
    )

    assert response["status"] == "failed"
    assert response["error"]

    await session.refresh(seeded.task)
    await session.refresh(seeded.result)
    assert seeded.task.status == "completed"
    assert seeded.task.error_code is None
    assert seeded.result.agent_status == "failed"
    assert seeded.result.agent_run_id == response["id"]
    assert seeded.result.agent_trace_id == response["trace_id"]
    assert seeded.result.agent_error == response["error"]

    run = await session.get(AgentRun, response["id"])
    assert run is not None and run.status == "failed"


@pytest.mark.asyncio
async def test_failure_can_retry_and_success_is_reused(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    fake_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    service = vision_service_cls(session)
    fake_llm.failures_remaining = 1

    first = await service.analyze_task(seeded.task.task_uid, user=admin_user)
    assert first["status"] == "failed"

    second = await service.analyze_task(seeded.task.task_uid, user=admin_user)
    assert second["status"] == "succeeded"
    assert second["id"] != first["id"]

    third = await service.analyze_task(seeded.task.task_uid, user=admin_user)
    assert third["id"] == second["id"]
    assert fake_llm.calls == 2
    assert await _succeeded_run_count(session, vision_agent.id) == 1

    await session.refresh(seeded.result)
    assert seeded.result.agent_status == "succeeded"
    assert seeded.result.agent_run_id == second["id"]
    assert seeded.result.agent_error is None


@pytest.mark.asyncio
async def test_stale_running_is_recovered_as_interrupted(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    fake_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    timeout_s = get_settings().vision_task_timeout_s
    seeded.result.agent_status = "running"
    seeded.result.agent_run_id = None
    seeded.result.updated_at = datetime.now(timezone.utc) - timedelta(
        seconds=timeout_s + 60
    )
    await session.commit()

    # 以 before_commit 快照观察实现是否先把 stale running 标成
    # failed(agent_interrupted) 再重试（该中间态可能被后续提交覆盖）。
    snapshots: list[tuple[str, str | None]] = []

    def _snapshot(_session) -> None:
        snapshots.append((seeded.result.agent_status, seeded.result.agent_error))

    event.listen(session.sync_session, "before_commit", _snapshot)
    try:
        response = await vision_service_cls(session).analyze_task(
            seeded.task.task_uid, user=admin_user
        )
    finally:
        event.remove(session.sync_session, "before_commit", _snapshot)

    assert response["status"] == "succeeded"
    assert any(error == "agent_interrupted" for _, error in snapshots), snapshots

    await session.refresh(seeded.result)
    assert seeded.result.agent_status == "succeeded"
    assert seeded.result.agent_run_id == response["id"]


@pytest.mark.asyncio
async def test_running_not_expired_returns_409(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    fake_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    seeded.result.agent_status = "running"
    seeded.result.updated_at = datetime.now(timezone.utc)
    await session.commit()

    with pytest.raises(Conflict) as excinfo:
        await vision_service_cls(session).analyze_task(
            seeded.task.task_uid, user=admin_user
        )

    assert (excinfo.value.detail or {}).get("reason") == "vision_agent_running"
    assert fake_llm.calls == 0


@pytest.mark.asyncio
async def test_analyze_event_uses_specified_event_then_reuses_success(
    session,
    admin_user,
    vision_agent,
    completed_task_with_events,
    fake_llm,
    vision_service_cls,
):
    seeded = completed_task_with_events
    service = vision_service_cls(session)

    response = await service.analyze_event(seeded.events[0].id, user=admin_user)
    assert response["status"] == "succeeded"

    prompt = json.dumps(fake_llm.last_task, ensure_ascii=False, default=str)
    assert OLD_EVENT_MARKER in prompt
    assert NEW_EVENT_MARKER not in prompt

    await session.refresh(seeded.result)
    assert seeded.result.agent_event_id == seeded.events[0].id

    again = await service.analyze_task(seeded.task.task_uid, user=admin_user)
    assert again["id"] == response["id"]
    # 换一条事件再 analyze 也不能新建 run：成功一次后整任务复用。
    other = await service.analyze_event(seeded.events[1].id, user=admin_user)
    assert other["id"] == response["id"]
    assert fake_llm.calls == 1


# ------------------------------------------------------------------------ HTTP


@pytest.mark.asyncio
async def test_agent_missing_returns_409(client, admin_token, completed_task_with_events):
    uid = completed_task_with_events.task.task_uid
    r = await client.post(
        f"/api/v1/vision/tasks/{uid}/analyze", headers=auth_headers(admin_token)
    )
    assert r.status_code == 409
    assert r.json()["code"] == 409
    assert r.json()["data"]["reason"] == "vision_agent_missing"


@pytest.mark.asyncio
async def test_no_events_returns_409(
    client, admin_token, vision_agent, completed_task_without_events
):
    uid = completed_task_without_events.task.task_uid
    r = await client.post(
        f"/api/v1/vision/tasks/{uid}/analyze", headers=auth_headers(admin_token)
    )
    assert r.status_code == 409
    assert r.json()["code"] == 409
    assert r.json()["data"]["reason"] == "no_vision_events"


@pytest.mark.asyncio
async def test_analyze_permissions_admin_and_operator_only(
    client,
    admin_token,
    operator_token,
    viewer_token,
    vision_agent,
    completed_task_with_events,
    fake_llm,
):
    uid = completed_task_with_events.task.task_uid
    url = f"/api/v1/vision/tasks/{uid}/analyze"

    event_id = completed_task_with_events.events[0].id
    event_url = f"/api/v1/vision/events/{event_id}/analyze"

    viewer = await client.post(url, headers=auth_headers(viewer_token))
    assert viewer.status_code == 403
    assert viewer.json()["code"] == 403
    viewer_event = await client.post(event_url, headers=auth_headers(viewer_token))
    assert viewer_event.status_code == 403
    assert viewer_event.json()["code"] == 403

    admin = await client.post(url, headers=auth_headers(admin_token))
    assert admin.status_code == 200
    assert admin.json()["code"] == 0
    assert admin.json()["data"]["status"] == "succeeded"

    operator = await client.post(url, headers=auth_headers(operator_token))
    assert operator.status_code == 200
    assert operator.json()["data"]["id"] == admin.json()["data"]["id"]

    operator_event = await client.post(event_url, headers=auth_headers(operator_token))
    assert operator_event.status_code == 200
    assert operator_event.json()["data"]["id"] == admin.json()["data"]["id"]


@pytest.mark.asyncio
async def test_http_failed_analysis_keeps_task_completed(
    client,
    admin_token,
    session,
    vision_agent,
    completed_task_with_events,
    failing_llm,
):
    seeded = completed_task_with_events
    r = await client.post(
        f"/api/v1/vision/tasks/{seeded.task.task_uid}/analyze",
        headers=auth_headers(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "failed"

    await session.refresh(seeded.task)
    assert seeded.task.status == "completed"


@pytest.mark.asyncio
async def test_cross_tenant_analyze_returns_404(
    client, other_tenant_token, completed_task_with_events
):
    uid = completed_task_with_events.task.task_uid
    r = await client.post(
        f"/api/v1/vision/tasks/{uid}/analyze",
        headers=auth_headers(other_tenant_token),
    )
    assert r.status_code == 404
    assert r.json()["code"] == 404


@pytest.mark.asyncio
async def test_agent_test_route_remains_admin_only(
    client, operator_token, viewer_token, vision_agent
):
    """既有 /agents/{id}/test 仍仅 admin：operator / viewer 均 403。"""
    for token in (operator_token, viewer_token):
        r = await client.post(
            f"/api/v1/agents/{vision_agent.id}/test",
            json={"task": {"question": "x"}},
            headers=auth_headers(token),
        )
        assert r.status_code == 403
        assert r.json()["code"] == 403


def test_llm_uses_agent_mock_fixture(vision_service_cls):
    """断言 ``config["mock_fixture"]`` 被透传进 ``LLMClient.fixture_name``。

    覆盖合法值原样透传，以及缺失/非法配置回落 ``None`` 的矩阵；
    优先级（显式 fixture 优先于 prompt hash）由
    ``tests/unit/agents/test_llm_client.py`` 覆盖。纯构造断言，不触 DB/网络。
    """
    from types import SimpleNamespace

    service = vision_service_cls(None)

    # 合法配置：原样透传，不能落回 prompt-hash 文件名。
    client = service._llm(SimpleNamespace(config={"mock_fixture": "demo_vision"}))
    assert client.fixture_name == "demo_vision"

    # 缺失 / 空 dict / config 属性不存在 / 非字符串 / 空白字符串：均为 None。
    for agent in (
        SimpleNamespace(config={}),
        SimpleNamespace(config=None),
        SimpleNamespace(),
        SimpleNamespace(config={"mock_fixture": 123}),
        SimpleNamespace(config={"mock_fixture": ""}),
        SimpleNamespace(config={"mock_fixture": "   "}),
    ):
        assert service._llm(agent).fixture_name is None
