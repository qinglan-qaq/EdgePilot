"""Vision Agent 研判服务（Task 18）：每任务成功一次 + 受控重试。

契约要点：

* 研判对象是**一个视觉任务**，输入只有「任务摘要 + 单条事件 + 模型/视频元数据」，
  绝不把 ``artifact["frames"]`` 逐帧喂给 LLM（帧级信息由推理服务负责）；
* 同一任务成功后只保留一条 ``agent_runs`` 行：第二次 analyze 直接按
  ``vision_results.agent_run_id`` 回读运行行重建响应，不新建 run、不再调 LLM；
* ``agent_status="running"`` 且结果行 ``updated_at`` 未超过
  ``vision_task_timeout_s`` 时返回 409 ``vision_agent_running``；超时视为上次
  进程崩溃，先落 ``failed(agent_interrupted)`` 再重试，避免永久卡死；
* Agent 失败**不改变** ``vision_tasks.status``，只写 ``agent_status / agent_error /
  agent_run_id / agent_trace_id / agent_event_id``；
* 返回字段与 ``POST /agents/{id}/test`` 完全一致，方便前端复用渲染逻辑。

本模块不做鉴权、不发 HTTP，全部由 ``app.api.v1.vision`` 的路由层负责。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentRunResult, run_agent
from app.agents.llm import LLMClient
from app.core.config import get_settings
from app.core.exceptions import Conflict, NotFound
from app.core.tenancy import get_tenant_id
from app.models.agent import Agent
from app.models.event import VisionEvent
from app.models.run import AgentRun
from app.models.vision import VisionResult, VisionTask
from app.observability import get_tracer
from app.repositories.vision_repo import VisionRepo

logger = logging.getLogger(__name__)

# vision_results.agent_status 的四个取值。
AGENT_STATUS_NONE = "none"
AGENT_STATUS_RUNNING = "running"
AGENT_STATUS_SUCCEEDED = "succeeded"
AGENT_STATUS_FAILED = "failed"

INTERRUPTED_ERROR = "agent_interrupted"
RUN_MISSING_ERROR = "agent_run_missing"

# 研判只读任务摘要，不提供任何帧级数据：instruction 明确禁止编造未提供的帧信息。
ANALYSIS_INSTRUCTION = (
    "基于任务摘要与单条事件给出研判结论；不要伪造未提供的帧级信息，"
    "证据不足时明确说明。"
)


class VisionAgentService:
    """视觉任务研判：单事件输入、任务级幂等、失败可重试。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = VisionRepo(session)

    # ------------------------------------------------------------------ public
    async def analyze_task(self, task_uid: str, *, user: Any = None) -> dict:
        """研判任务的最新事件；成功后整任务复用同一次运行。"""
        tenant_id = self._tenant_id(user)
        task, result = await self._load_task_result(tenant_id, task_uid)
        return await self._analyze(
            task,
            result,
            tenant_id=tenant_id,
            event_loader=lambda: self.repo.latest_event(tenant_id, task.task_uid),
        )

    async def analyze_event(self, event_id: int, *, user: Any = None) -> dict:
        """研判指定事件（仍归属其任务）；成功复用规则与 ``analyze_task`` 相同。"""
        tenant_id = self._tenant_id(user)
        event = await self.repo.get_event(tenant_id, int(event_id))
        if event is None:
            raise NotFound("视觉事件不存在", detail={"reason": "event_not_found"})

        task_uid = self._event_task_uid(event)
        task, result = await self._load_task_result(tenant_id, task_uid)
        return await self._analyze(
            task,
            result,
            tenant_id=tenant_id,
            event_loader=self._fixed_event(event),
        )

    # ----------------------------------------------------------------- helpers
    def _llm(self, agent: Agent) -> LLMClient:
        """LLM 客户端 seam：测试 monkeypatch 本方法注入替身，默认按配置构造。

        Agent 的 ``config["mock_fixture"]`` 是 mock 模式的显式 fixture 名，
        与 supervisor / workflow / agents 路由保持一致。仅在未配置合法
        ``mock_fixture`` 时，才由 LLMClient 回落到 prompt-hash 文件名；
        该回落只适用于调用方本就未指定 fixture 的场景。
        """
        settings = get_settings()
        config = getattr(agent, "config", None)
        fixture_name = config.get("mock_fixture") if isinstance(config, dict) else None
        if not (isinstance(fixture_name, str) and fixture_name.strip()):
            fixture_name = None
        return LLMClient(
            mode=settings.llm_mode,
            fixture_dir=settings.llm_fixture_dir,
            fixture_name=fixture_name,
        )

    @staticmethod
    def _tenant_id(user: Any) -> uuid.UUID:
        """请求用户自带租户；服务层直调时退回上下文租户。"""
        tenant = getattr(user, "tenant_id", None)
        return tenant if tenant is not None else get_tenant_id()

    async def _load_task_result(
        self, tenant_id: uuid.UUID, task_uid: str
    ) -> tuple[VisionTask, VisionResult]:
        """按租户加载任务与结果行并加行锁；缺失统一 404。"""
        task = await self.repo.get_task(tenant_id, task_uid, for_update=True)
        if task is None:
            raise NotFound("任务不存在", detail={"reason": "task_not_found"})
        result = await self.repo.get_result(tenant_id, task.id, for_update=True)
        if result is None:
            raise NotFound("任务结果尚未生成", detail={"reason": "result_not_found"})
        return task, result

    @staticmethod
    def _event_task_uid(event: VisionEvent) -> str:
        """事件通过 ``payload.task_uid`` 关联任务；缺失视为任务不存在。"""
        payload = event.payload if isinstance(event.payload, dict) else {}
        task_uid = payload.get("task_uid")
        if not isinstance(task_uid, str) or not task_uid:
            raise NotFound("视觉事件未关联任务", detail={"reason": "task_not_found"})
        return task_uid

    def _fixed_event(self, event: VisionEvent):
        """``analyze_event`` 使用指定事件，不再回查最新一条。"""

        async def _loader() -> VisionEvent:
            return event

        return _loader

    async def _analyze(
        self,
        task: VisionTask,
        result: VisionResult,
        *,
        tenant_id: uuid.UUID,
        event_loader,
    ) -> dict:
        """共用主流程：复用 → running 检查 → 事件/Agent 校验 → 单次运行。"""
        settings = get_settings()
        if not settings.vision_agent_enabled:
            raise Conflict(
                "视觉研判已关闭", detail={"reason": "vision_agent_disabled"}
            )
        if (
            settings.vision_agent_once_per_task
            and result.agent_status == AGENT_STATUS_SUCCEEDED
            and result.agent_run_id
        ):
            reused = await self._reuse_response(tenant_id, int(result.agent_run_id))
            if reused is not None:
                return reused
            # 运行行已不存在（被清理/租户不符）：落失败态后重新研判。
            logger.warning(
                "视觉研判运行行缺失，重新研判 task_uid=%s agent_run_id=%s",
                task.task_uid,
                result.agent_run_id,
            )
            result.agent_status = AGENT_STATUS_FAILED
            result.agent_error = RUN_MISSING_ERROR

        if result.agent_status == AGENT_STATUS_RUNNING:
            if not self._running_expired(result):
                raise Conflict("研判正在进行", detail={"reason": "vision_agent_running"})
            # 上次进程中断：先落可观测的失败态并提交，再按正常失败流程重试。
            logger.warning(
                "视觉研判超时未收敛，标记中断并重试 task_uid=%s", task.task_uid
            )
            result.agent_status = AGENT_STATUS_FAILED
            result.agent_error = INTERRUPTED_ERROR
            await self.session.commit()

        event = await event_loader()
        if event is None:
            raise Conflict("任务没有可研判事件", detail={"reason": "no_vision_events"})

        agent = await self._select_agent(tenant_id)
        if agent is None:
            raise Conflict("租户内没有可用的视觉 Agent", detail={"reason": "vision_agent_missing"})

        result.agent_status = AGENT_STATUS_RUNNING
        result.agent_error = None
        await self.session.commit()

        payload = self._build_payload(task, result, event)
        run = await run_agent(
            agent,
            payload,
            tracer=get_tracer(),
            llm=self._llm(agent),
            session=self.session,
            tenant_id=tenant_id,
        )

        result.agent_status = run.status
        result.agent_run_id = run.agent_run_id
        result.agent_event_id = event.id
        result.agent_error = run.error
        result.agent_trace_id = run.trace_id
        await self.session.commit()

        return self._to_response(agent, run)

    def _running_expired(self, result: VisionResult) -> bool:
        """``running`` 是否已超过 ``vision_task_timeout_s`` 未更新。"""
        timeout_s = get_settings().vision_task_timeout_s
        updated = result.updated_at
        if updated is None:
            return True
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated > timedelta(seconds=timeout_s)

    async def _select_agent(self, tenant_id: uuid.UUID) -> Agent | None:
        """租户内第一个 ``agent_type='vision'`` 且启用的 Agent。"""
        return await self.session.scalar(
            select(Agent)
            .where(
                Agent.tenant_id == tenant_id,
                Agent.agent_type == "vision",
                Agent.status == "active",
            )
            .order_by(Agent.id)
            .limit(1)
        )

    def _build_payload(
        self, task: VisionTask, result: VisionResult, event: VisionEvent
    ) -> dict:
        """任务摘要 + 单条事件 + 模型/视频元数据；不含任何帧级数据。"""
        return {
            "task": {
                "task_uid": task.task_uid,
                "video_filename": task.video_filename,
                "summary": result.summary,
                "model": task.model_name,
                "model_version": task.model_version,
                "video": {
                    "width": task.video_width,
                    "height": task.video_height,
                    "fps": self._float(task.video_fps),
                    "duration_ms": task.video_duration_ms,
                    "frame_count": task.frame_count,
                },
            },
            "event": {
                "event_type": event.event_type,
                "severity": event.severity,
                "confidence": self._float(event.confidence) or 0.0,
                "occurred_at": self._iso(event.occurred_at),
                "bbox": event.bbox,
                "payload": event.payload,
            },
            "instruction": ANALYSIS_INSTRUCTION,
        }

    async def _reuse_response(
        self, tenant_id: uuid.UUID, agent_run_id: int
    ) -> dict | None:
        """按既有 ``agent_runs`` 行重建响应；缺失或跨租户返回 None。"""
        run = await self.session.get(AgentRun, agent_run_id)
        if run is None or run.tenant_id != tenant_id:
            return None
        return {
            "id": run.id,
            "agent_id": run.agent_id,
            "agent_version": run.agent_version,
            "status": run.status,
            "output": run.output,
            "error": run.error,
            "trace_id": run.trace_id,
            "span_id": run.span_id,
            "total_tokens": run.total_tokens,
            "total_cost": self._cost(run.total_cost),
            "retry_count": run.retry_count,
        }

    @staticmethod
    def _to_response(agent: Agent, run: AgentRunResult) -> dict:
        """与 ``POST /agents/{id}/test`` 逐字段一致的响应。"""
        return {
            "id": run.agent_run_id,
            "agent_id": agent.id,
            "agent_version": agent.current_version,
            "status": run.status,
            "output": run.output,
            "error": run.error,
            "trace_id": run.trace_id,
            "span_id": run.span_id,
            "total_tokens": run.total_tokens,
            "total_cost": VisionAgentService._cost(run.total_cost),
            "retry_count": run.retry_count,
        }

    @staticmethod
    def _cost(value: Any) -> str | None:
        """与 agents 路由一致：``Decimal`` 字符串化，缺失为 None。"""
        return str(value) if value is not None else None

    @staticmethod
    def _float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if isinstance(value, datetime) else None


__all__ = [
    "AGENT_STATUS_FAILED",
    "AGENT_STATUS_NONE",
    "AGENT_STATUS_RUNNING",
    "AGENT_STATUS_SUCCEEDED",
    "VisionAgentService",
]
