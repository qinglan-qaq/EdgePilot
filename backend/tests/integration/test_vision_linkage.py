"""Task 24 联动集成验收：一次真实 ingest 串起任务、事件、Run 与 Agent 证据链。

本文件只使用测试内直接插库的「已发布工作流 + vision Agent」本地夹具
（参考 ``tests/integration`` 既有种子方式），不依赖外部 seed 脚本。

关键约定（plan Task 12/17/18）：

* ``VisionResultService.ingest`` 按「结果 → 事件 → completed → 自动触发」执行；
* 触发对同一 ``vision_task_uid`` 只创建一个 event Run；
* Agent 研判每任务成功一次，第二次复用既有 ``agent_run``；
* ``vision_results`` 的 task_uid / agent_event_id / workflow_run_id /
  agent_run_id / agent_trace_id 与 ``agent_runs.trace_id`` 串成一条链。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.agents.llm import LLMResponse
from app.core.config import get_settings
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.agent import Agent
from app.models.event import VisionEvent
from app.models.run import AgentRun, WorkflowRun
from app.models.vision import VisionResult, VisionTask
from app.models.workflow import Workflow, WorkflowVersion
from app.services.vision_agent_service import VisionAgentService
from app.services.vision_result_service import VisionResultService
from app.services.vision_storage import RESULT_SCHEMA_VERSION, artifact_digest
from app.services.vision_trigger_service import VisionTriggerService
from app.workflow.dsl import dsl_hash
from tests.conftest import TENANT


# ---------------------------------------------------------------- artifact 夹具


def _person_artifact(uid: str) -> dict:
    """一个可生成事件的最小真实 artifact：person 置信度高于 0.6 阈值。"""
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "event_id": uid,
        "event_type": "vision.video_detection",
        "timestamp": "2026-11-26T10:00:00+00:00",
        "device_id": "video-upload",
        "source": {
            "type": "video_file",
            "video_id": uid,
            "filename": "gate.mp4",
            "width": 640,
            "height": 360,
            "fps": 25.0,
            "duration_ms": 2000,
        },
        "video": {
            "width": 640,
            "height": 360,
            "fps": 25.0,
            "duration_ms": 2000,
            "sampled_every_n_frames": 1,
            "processed_frames": 2,
            "timestamp_source": "fps",
            "max_samples": 2000,
        },
        "model": {
            "provider": "ultralytics",
            "model_name": "yolo11n",
            "model_version": "yolo11n-0123456789ab",
            "device": "cpu",
            "imgsz": 640,
            "license": "AGPL-3.0",
        },
        "summary": {
            "processed_frames": 2,
            "detection_count": 1,
            "class_counts": {"person": 1},
            "peak_confidence": 0.9,
            "inference_ms": 10,
        },
        "frames": [
            {
                "frame_id": 0,
                "timestamp_ms": 0,
                "inference_ms": 5.0,
                "detections": [
                    {
                        "detection_id": "0-0",
                        "class_id": 0,
                        "class_name": "person",
                        "confidence": 0.9,
                        "bbox": {"x1": 10.0, "y1": 20.0, "x2": 110.0, "y2": 220.0},
                    }
                ],
            },
            {"frame_id": 25, "timestamp_ms": 1000, "inference_ms": 5.0, "detections": []},
        ],
        "error": None,
    }


async def _make_task(session, *, uid: str | None = None) -> VisionTask:
    uid = uid or str(uuid.uuid4())
    digest = hashlib.sha256(uid.encode("utf-8")).hexdigest()
    task = VisionTask(
        tenant_id=TENANT,
        task_uid=uid,
        dedup_key=digest,
        status="running",
        source_type="upload",
        video_filename="gate.mp4",
        video_path=f"videos/{TENANT}/{uid}.mp4",
        video_sha256=digest,
        file_size_bytes=2048,
        content_type="video/mp4",
        conf_threshold=Decimal("0.250"),
        frame_stride=1,
        target_samples=900,
        model_name="yolo11n",
        model_version="yolo11n-0123456789ab",
        created_by=1,
    )
    session.add(task)
    await session.flush()
    return task


def _state_for(uid: str, artifact: dict, file_bytes: bytes) -> dict:
    """模拟 vision-service 的 completed 状态；字段与 JobManager state 对齐。"""
    return {
        "task_uid": uid,
        "status": "completed",
        "progress": 100,
        "attempt": 1,
        "result_available": True,
        "result_bytes": len(file_bytes),
        "result_file_sha256": hashlib.sha256(file_bytes).hexdigest(),
        "result_sha256": artifact_digest(artifact),
        "result_schema_version": artifact["schema_version"],
        "processed_frames": artifact["summary"]["processed_frames"],
        "detection_count": artifact["summary"]["detection_count"],
        "video": artifact["video"],
        "model": artifact["model"],
        "summary": artifact["summary"],
        "error_code": None,
        "error_message": None,
    }


def _write_artifact(root: Path, uid: str, artifact: dict) -> tuple[Path, dict]:
    path = root / "jobs" / uid / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(artifact, ensure_ascii=False).encode("utf-8")
    path.write_bytes(data)
    return path, _state_for(uid, artifact, data)


async def _complete_task(session, root: Path) -> tuple[VisionTask, VisionResult, dict]:
    """真实走 VisionResultService.ingest：结果 → 事件 → completed → 自动触发。"""
    uid = str(uuid.uuid4())
    artifact = _person_artifact(uid)
    task = await _make_task(session, uid=uid)
    _, state = _write_artifact(root, uid, artifact)
    result = await VisionResultService(session).ingest(task, state)
    return task, result, state


# ---------------------------------------------------------------- 查询辅助


async def _events_for(session, task_uid: str) -> list[VisionEvent]:
    rows = await session.scalars(
        select(VisionEvent)
        .where(
            VisionEvent.tenant_id == TENANT,
            VisionEvent.payload["task_uid"].astext == task_uid,
        )
        .order_by(VisionEvent.id)
    )
    return list(rows.all())


async def _event_count(session, task_uid: str) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(VisionEvent)
            .where(
                VisionEvent.tenant_id == TENANT,
                VisionEvent.payload["task_uid"].astext == task_uid,
            )
        )
        or 0
    )


async def _runs_for(session, task_uid: str) -> list[WorkflowRun]:
    rows = await session.scalars(
        select(WorkflowRun).where(
            WorkflowRun.tenant_id == TENANT,
            WorkflowRun.trigger_type == "event",
            WorkflowRun.trigger_payload["vision_task_uid"].astext == task_uid,
        )
    )
    return list(rows.all())


async def _agent_run_by_id(session, agent_run_id: int | None) -> AgentRun | None:
    if agent_run_id is None:
        return None
    return await session.scalar(
        select(AgentRun).where(
            AgentRun.tenant_id == TENANT, AgentRun.id == agent_run_id
        )
    )


async def _agent_run_count(session, agent_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.tenant_id == TENANT, AgentRun.agent_id == agent_id)
        )
        or 0
    )


# ---------------------------------------------------------------- 本地种子夹具


class _FakeLLM:
    """最小 LLM 替身：只记录调用次数，不触网、不伪造推理结果。"""

    def __init__(self) -> None:
        self.calls = 0

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
        payload = {
            "summary": "检测到人员进入警戒区，建议现场核实。",
            "events": [{"event_type": "vision.detection", "severity": "medium"}],
            "evidence": [
                {"type": "event", "ref": "vision_event", "snippet": "person bbox"}
            ],
        }
        return LLMResponse(
            content=json.dumps(payload, ensure_ascii=False),
            usage={"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            finish_reason="stop",
            provider="mock",
            model_name="mock",
        )


@pytest.fixture(autouse=True)
def deterministic_runtime(monkeypatch):
    """开启观测（trace_id 可断言）并建立直调 service 所需的租户上下文。"""
    import app.observability as obs

    monkeypatch.setenv("OBS_ENABLED", "true")
    get_settings.cache_clear()
    obs.reset_tracer()

    token = set_tenant_id(TENANT)
    try:
        yield TENANT
    finally:
        reset_tenant_id(token)
        obs.reset_tracer()
        get_settings.cache_clear()


@pytest.fixture
def vision_root(tmp_path, monkeypatch):
    """把 VISION_STORAGE_DIR 指到临时目录，artifact 读取走真实文件。"""
    monkeypatch.setattr(get_settings(), "vision_storage_dir", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def deterministic_llm(monkeypatch):
    """联动验收只使用 mock LLM；不访问真实模型，也不伪造推理结果。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_mode", "mock")
    monkeypatch.setattr(settings, "vision_trigger_enabled", True)
    monkeypatch.setattr(settings, "vision_agent_enabled", True)
    monkeypatch.setattr(settings, "vision_agent_once_per_task", True)


@pytest.fixture
def fake_llm(monkeypatch):
    """按 Task 18 的 ``self._llm(agent)`` 契约注入确定性 LLM。"""
    fake = _FakeLLM()
    monkeypatch.setattr(
        VisionAgentService, "_llm", lambda *args, **kwargs: fake, raising=False
    )
    return fake


@pytest.fixture
async def seeded_workflow(session) -> tuple[Workflow, WorkflowVersion]:
    """直接插库的已发布同名工作流（不依赖 seed 脚本）。"""
    dsl = {"nodes": [{"id": "start", "type": "start"}], "edges": []}
    workflow = Workflow(
        tenant_id=TENANT,
        name=get_settings().vision_trigger_workflow_name,
        description="联动验收夹具",
        status="published",
        current_version=1,
        created_by=1,
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        tenant_id=TENANT,
        workflow_id=workflow.id,
        version=1,
        dsl=dsl,
        dsl_hash=dsl_hash(dsl),
        created_by=1,
    )
    session.add(version)
    await session.flush()
    return workflow, version


@pytest.fixture
async def vision_agent(session) -> Agent:
    """直接插库的 vision Agent；fixture 名指向 llm 夹具目录中的确定性响应。"""
    agent = Agent(
        tenant_id=TENANT,
        name=f"vision-analyst-{uuid.uuid4().hex[:8]}",
        agent_type="vision",
        status="active",
        current_version=1,
        config={
            "system_prompt": "你是视觉事件研判助手，只基于任务摘要与单条事件作答。",
            "mock_fixture": "device_ok",
        },
    )
    session.add(agent)
    await session.flush()
    return agent


# ---------------------------------------------------------------- 验收用例


async def test_complete_task_triggers_one_run_and_one_agent(
    session, vision_root, seeded_workflow, vision_agent, seeded_users, fake_llm
):
    """一次完成只产生 1 个 Run 与 1 个 AgentRun；重放调用不新增。"""
    admin = seeded_users[0]
    task, result, state = await _complete_task(session, vision_root)
    await session.refresh(task)
    await session.refresh(result)

    assert task.status == "completed"
    assert result.trigger_status == "triggered"
    assert result.workflow_run_id is not None
    assert result.agent_run_id is None, "自动触发工作流不等于自动研判"

    runs = await _runs_for(session, task.task_uid)
    assert len(runs) == 1
    assert runs[0].id == result.workflow_run_id
    assert runs[0].trigger_payload["vision_task_uid"] == task.task_uid
    assert await _event_count(session, task.task_uid) == 1

    # 重放：直接调用触发与重复 ingest 都只命中幂等分支。
    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "triggered"
    assert outcome.workflow_run_id == runs[0].id
    replayed = await VisionResultService(session).ingest(task, state)
    assert replayed.id == result.id
    assert await _event_count(session, task.task_uid) == 1, "重放不得新增事件"
    assert len(await _runs_for(session, task.task_uid)) == 1, "重放不得新增 Run"

    # 研判两次：第二次必须复用同一个 agent_run。
    first = await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    second = await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    assert first["status"] == "succeeded"
    assert second["id"] == first["id"]
    assert fake_llm.calls == 1, "第二次研判必须复用结果，不得再次调用 LLM"
    assert await _agent_run_count(session, vision_agent.id) == 1, "研判不得新建第二个 AgentRun"

    await session.refresh(result)
    assert result.agent_run_id is not None
    assert result.agent_event_id is not None
    assert result.agent_trace_id

    agent_run = await _agent_run_by_id(session, result.agent_run_id)
    assert agent_run is not None
    assert agent_run.agent_id == vision_agent.id
    assert agent_run.status == "succeeded"
    assert agent_run.trace_id == result.agent_trace_id
    assert len(await _runs_for(session, task.task_uid)) == 1


async def test_linkage_chain_fields_reference_same_task_uid_and_trace(
    session, vision_root, seeded_workflow, vision_agent, seeded_users, fake_llm
):
    """task_uid → 事件 payload → Run trigger_payload → 结果指针 → trace 一条链。"""
    admin = seeded_users[0]
    task, result, _ = await _complete_task(session, vision_root)
    response = await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    assert response["status"] == "succeeded"
    assert fake_llm.calls == 1
    await session.refresh(result)

    events = await _events_for(session, task.task_uid)
    assert len(events) == 1
    event = events[0]
    runs = await _runs_for(session, task.task_uid)
    assert len(runs) == 1
    run = runs[0]
    agent_run = await _agent_run_by_id(session, result.agent_run_id)
    assert agent_run is not None

    # 任务 → 事件
    assert result.task_id == task.id
    assert result.task_uid == task.task_uid
    assert event.payload["task_uid"] == task.task_uid
    assert result.agent_event_id == event.id

    # 任务 → 工作流 Run
    assert run.trigger_payload["vision_task_uid"] == task.task_uid
    assert run.trigger_payload["vision_task_id"] == task.id
    assert run.trigger_payload["vision_event_ids"] == [event.id]
    assert run.trigger_payload["first_event_id"] == event.id
    assert run.input["task_uid"] == task.task_uid
    assert result.workflow_run_id == run.id

    # 任务 → AgentRun → trace
    assert result.agent_run_id == agent_run.id == response["id"]
    assert result.agent_status == agent_run.status == "succeeded"
    assert result.agent_trace_id == agent_run.trace_id == response["trace_id"]
    assert agent_run.trace_id, "AgentRun 必须带可追溯的 trace_id"
