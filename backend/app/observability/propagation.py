"""跨进程 trace 上下文传播（W3C ``traceparent`` / Celery / MQTT / 线程池）。

设计约束（spec §2.3）：

- ``traceparent`` 只解析/格式化，**绝不伪造** id；没有活动 trace 时返回
  ``None``，由调用方决定是否生成（下行指令的显式生成仍由
  ``app.messaging.mqtt.traceparent`` 负责）。
- Celery 使用私有 kwargs ``__trace__`` 携带上下文；``task_prerun`` 恢复，
  ``task_postrun`` 清理。任何传播失败只记日志，不能影响任务执行。
- MQTT 使用载荷字段 ``traceparent`` 而不是 v5 User Property：现场 broker /
  旧固件只支持 MQTT 3.1.1，属性会丢失；载荷字段可被 agent 原样透传。
- 线程池必须用 :func:`submit_with_context` / :func:`bind_context`，禁止直接
  ``executor.submit`` 丢掉 ``contextvars``。
"""

from __future__ import annotations

import contextvars
import functools
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from celery.signals import task_postrun, task_prerun

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.observability import context as obs_context

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Celery 保留 kwargs 键：只用于跨进程传播，不属于任务业务参数。
TRACE_KWARG = "__trace__"

_VERSION = "00"
_DEFAULT_FLAGS = "01"
_ZERO_TRACE = "0" * 32
_ZERO_SPAN = "0" * 16

_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")


# ------------------------------------------------------------------ traceparent


@dataclass(frozen=True)
class TraceParent:
    """解析后的 W3C ``traceparent`` 头。"""

    trace_id: str
    span_id: str
    flags: str = _DEFAULT_FLAGS
    version: str = _VERSION


def is_valid_trace_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_TRACE_ID_RE.match(value))
        and value != _ZERO_TRACE
    )


def is_valid_span_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(_SPAN_ID_RE.match(value))
        and value != _ZERO_SPAN
    )


def parse_traceparent(value: Any) -> TraceParent | None:
    """解析 ``00-<32hex>-<16hex>-<2hex>``；非法值返回 ``None``。

    拒绝：非字符串、版本不是 ``00``、长度/分隔符错误、非小写 hex、
    trace_id/span_id 全零。不做任何猜测或补全。
    """
    if not isinstance(value, str):
        return None
    match = _TRACEPARENT_RE.match(value)
    if match is None:
        return None
    if match.group("version") != _VERSION:
        return None
    trace_id = match.group("trace_id")
    span_id = match.group("span_id")
    if not is_valid_trace_id(trace_id) or not is_valid_span_id(span_id):
        return None
    return TraceParent(
        trace_id=trace_id,
        span_id=span_id,
        flags=match.group("flags"),
        version=match.group("version"),
    )


def format_traceparent(
    trace_id: Any, span_id: Any, flags: str | int = _DEFAULT_FLAGS
) -> str:
    """格式化 W3C ``traceparent``；非法输入抛 ``ValueError``（不伪造 id）。"""
    if not is_valid_trace_id(trace_id):
        raise ValueError(f"非法 trace_id: {trace_id!r}")
    if not is_valid_span_id(span_id):
        raise ValueError(f"非法 span_id: {span_id!r}")
    if isinstance(flags, int):
        if not 0 <= flags <= 0xFF:
            raise ValueError(f"非法 flags: {flags!r}")
        flags_text = f"{flags:02x}"
    elif isinstance(flags, str) and re.fullmatch(r"[0-9a-f]{2}", flags):
        flags_text = flags
    else:
        raise ValueError(f"非法 flags: {flags!r}")
    return f"{_VERSION}-{trace_id}-{span_id}-{flags_text}"


# ------------------------------------------------------------ 跨进程上下文对象


@dataclass(frozen=True)
class TraceContext:
    """可跨进程序列化的最小 trace 上下文（不含任何密钥）。"""

    trace_id: str
    parent_span_id: str | None = None
    root_span_id: str | None = None
    tenant_id: uuid.UUID | None = None
    name: str | None = None
    source: str | None = None
    capture_mode: str | None = None

    @classmethod
    def from_payload(cls, payload: Any) -> "TraceContext | None":
        """从 Celery ``__trace__`` kwargs 恢复；非法 payload 返回 ``None``。"""
        if not isinstance(payload, dict):
            return None
        trace_id = payload.get("trace_id")
        if not is_valid_trace_id(trace_id):
            return None

        parent = payload.get("parent_span_id")
        if not is_valid_span_id(parent):
            parent = None
        root = payload.get("root_span_id")
        if not is_valid_span_id(root):
            root = None

        tenant = payload.get("tenant_id")
        if tenant is not None and not isinstance(tenant, uuid.UUID):
            try:
                tenant = uuid.UUID(str(tenant))
            except (ValueError, AttributeError, TypeError):
                tenant = None

        name = payload.get("name")
        source = payload.get("source")
        capture_mode = payload.get("capture_mode")
        return cls(
            trace_id=trace_id,
            parent_span_id=parent,
            root_span_id=root,
            tenant_id=tenant,
            name=name if isinstance(name, str) else None,
            source=source if isinstance(source, str) else None,
            capture_mode=capture_mode if isinstance(capture_mode, str) else None,
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"trace_id": self.trace_id}
        if self.parent_span_id:
            payload["parent_span_id"] = self.parent_span_id
        if self.root_span_id:
            payload["root_span_id"] = self.root_span_id
        if self.tenant_id is not None:
            payload["tenant_id"] = str(self.tenant_id)
        if self.name:
            payload["name"] = self.name
        if self.source:
            payload["source"] = self.source
        if self.capture_mode:
            payload["capture_mode"] = self.capture_mode
        return payload


def trace_context_from_current() -> TraceContext | None:
    """只从当前 ``contextvars`` 读取；无活动 trace 时返回 ``None``。"""
    trace = obs_context.current_trace.get()
    trace_id = getattr(trace, "trace_id", None)
    if not is_valid_trace_id(trace_id):
        # NullTrace / 空上下文：不生成、不猜测
        return None

    span = obs_context.current_span.get()
    parent_span_id = getattr(span, "span_id", None) if span is not None else None
    if not is_valid_span_id(parent_span_id):
        parent_span_id = None
    # 只接受属于同一 trace 的 span，避免把外层 span 挂到内层 trace
    if (
        parent_span_id is not None
        and getattr(span, "trace_id", trace_id) != trace_id
    ):
        parent_span_id = None

    root_span_id = getattr(trace, "root_span_id", None)
    if not is_valid_span_id(root_span_id):
        root_span_id = None

    tenant_id = getattr(trace, "tenant_id", None)
    if not isinstance(tenant_id, uuid.UUID):
        try:
            tenant_id = uuid.UUID(str(tenant_id)) if tenant_id else None
        except (ValueError, AttributeError, TypeError):
            tenant_id = None

    tracer = getattr(trace, "tracer", None)
    capture_mode = getattr(tracer, "capture_mode", None)
    name = getattr(trace, "name", None)
    source = getattr(trace, "source", None)

    return TraceContext(
        trace_id=trace_id,
        parent_span_id=parent_span_id,
        root_span_id=root_span_id,
        tenant_id=tenant_id,
        name=name if isinstance(name, str) else None,
        source=source if isinstance(source, str) else None,
        capture_mode=capture_mode if isinstance(capture_mode, str) else None,
    )


def trace_payload_from_current() -> dict[str, Any] | None:
    """构造 Celery 注入用 payload；无活动 trace 时返回 ``None``。"""
    ctx = trace_context_from_current()
    return ctx.to_payload() if ctx is not None else None


def current_traceparent() -> str | None:
    """当前上下文对应的 ``traceparent``；无 trace 或无可用 span 时返回 ``None``。"""
    ctx = trace_context_from_current()
    if ctx is None:
        return None
    span_id = ctx.parent_span_id or ctx.root_span_id
    if not is_valid_span_id(span_id):
        return None
    return format_traceparent(ctx.trace_id, span_id, _DEFAULT_FLAGS)


def add_traceparent_to_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """给下行消息载荷补 ``traceparent``；已有值不覆盖，无 trace 不添加。

    MQTT 3.1.1 没有 User Property，载荷字段对现场 broker / 旧固件向后兼容。
    任何异常都吞掉，绝不影响发布。
    """
    try:
        if not isinstance(payload, dict) or payload.get("traceparent"):
            return payload
        traceparent = current_traceparent()
        if traceparent is not None:
            payload["traceparent"] = traceparent
    except Exception:  # noqa: BLE001 - 传播失败不能影响业务发布
        logger.warning("MQTT traceparent 注入失败", exc_info=True)
    return payload


# ----------------------------------------------------------- 远端 trace / span


@dataclass
class RemoteSpan:
    """恢复出来的远端父 span；只暴露 id 并让观测方法安全 no-op。"""

    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str = "remote.parent"
    span_type: str = "remote"

    def set_input(self, value: Any) -> "RemoteSpan":
        return self

    def set_output(self, value: Any) -> "RemoteSpan":
        return self

    def set_error(self, code: str, message: str, *, retryable: bool = False) -> "RemoteSpan":
        return self

    def set_meta(self, **kv: Any) -> "RemoteSpan":
        return self

    def add_event(self, event_type: str, data: dict) -> "RemoteSpan":
        return self

    def record_llm_call(self, rec) -> "RemoteSpan":
        return self

    def record_tool_call(self, rec) -> "RemoteSpan":
        return self

    def record_retrieval(self, rec) -> "RemoteSpan":
        return self

    def finish(self, status: str = "succeeded") -> None:
        return None

    async def __aenter__(self) -> "RemoteSpan":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@dataclass
class RemoteTrace:
    """恢复出来的远端 trace；租户/id 供 ``Tracer.start_span`` 正确挂接。"""

    trace_id: str
    root_span_id: str
    tenant_id: uuid.UUID | None = None
    name: str | None = None
    source: str | None = None
    tracer: Any = None

    def set_output(self, value: Any) -> "RemoteTrace":
        return self

    def close(self, status: str = "succeeded") -> None:
        return None

    async def __aenter__(self) -> "RemoteTrace":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


# --------------------------------------------------------------- Celery 信号

# 栈元素：(上层状态, 本层 token 列表, tenant token, task_id)。token 必须在同一 context 内 reset。
_RESTORE_STACK: contextvars.ContextVar[tuple | None] = contextvars.ContextVar(
    "obs_propagation_restore_stack", default=None
)


def _restore_context(ctx: TraceContext, task_id: str | None = None) -> None:
    prev_tenant_token = None

    remote_trace = RemoteTrace(
        trace_id=ctx.trace_id,
        root_span_id=ctx.root_span_id or "",
        tenant_id=ctx.tenant_id,
        name=ctx.name,
        source=ctx.source,
    )
    remote_span = None
    if ctx.parent_span_id:
        remote_span = RemoteSpan(
            trace_id=ctx.trace_id,
            span_id=ctx.parent_span_id,
        )

    tokens: list[tuple[contextvars.ContextVar, contextvars.Token]] = []
    try:
        tokens.append((obs_context.current_trace, obs_context.current_trace.set(remote_trace)))
        tokens.append((obs_context.current_span, obs_context.current_span.set(remote_span)))
        tokens.append((obs_context.sequence, obs_context.sequence.set(0)))
        if ctx.tenant_id is not None:
            prev_tenant_token = set_tenant_id(ctx.tenant_id)
    except Exception:  # noqa: BLE001 - 恢复失败要回退已设置的 token
        _reset_tokens(tokens)
        if prev_tenant_token is not None:
            _safe_reset_tenant(prev_tenant_token)
        raise

    stack = _RESTORE_STACK.get()
    _RESTORE_STACK.set((stack, tokens, prev_tenant_token, task_id))


def _reset_tokens(tokens: list[tuple[contextvars.ContextVar, contextvars.Token]]) -> None:
    for var, token in reversed(tokens):
        try:
            var.reset(token)
        except (ValueError, RuntimeError):
            # token 属于另一个 context：保留当前值比抛错更安全
            logger.debug("trace token reset 失败（context 不一致）", exc_info=True)


def _safe_reset_tenant(token: contextvars.Token) -> None:
    try:
        reset_tenant_id(token)
    except (ValueError, RuntimeError):
        logger.debug("tenant token reset 失败（context 不一致）", exc_info=True)


def _clear_context(task_id: str | None = None) -> None:
    stack = _RESTORE_STACK.get()
    if stack is None:
        return
    prev_stack, tokens, prev_tenant_token, restore_task_id = stack
    # 内联子任务的 postrun 不能清掉外层任务的上下文
    if (
        restore_task_id is not None
        and task_id is not None
        and restore_task_id != task_id
    ):
        return
    _reset_tokens(tokens)
    if prev_tenant_token is not None:
        _safe_reset_tenant(prev_tenant_token)
    _RESTORE_STACK.set(prev_stack)


def on_task_prerun(
    sender=None, task_id=None, task=None, args=None, kwargs=None, **_extra: Any
) -> None:
    """Celery ``task_prerun``：有 ``__trace__`` 时恢复 trace / span / tenant。"""
    try:
        if not isinstance(kwargs, dict):
            return
        payload = kwargs.get(TRACE_KWARG)
        if not payload:
            return
        ctx = TraceContext.from_payload(payload)
        if ctx is None:
            logger.debug("忽略非法 __trace__ payload（task=%s）", getattr(task, "name", task))
            return
        _restore_context(ctx, task_id=task_id)
    except Exception:  # noqa: BLE001 - 传播失败不能影响任务执行
        logger.warning(
            "Celery trace 上下文恢复失败（task=%s）",
            getattr(task, "name", task),
            exc_info=True,
        )


def on_task_postrun(
    sender=None,
    task_id=None,
    task=None,
    args=None,
    kwargs=None,
    retval=None,
    state=None,
    **_extra: Any,
) -> None:
    """Celery ``task_postrun``：清理本任务恢复的 trace 上下文。"""
    try:
        _clear_context(task_id=task_id)
    except Exception:  # noqa: BLE001 - 清理失败只记日志
        logger.warning(
            "Celery trace 上下文清理失败（task=%s）",
            getattr(task, "name", task),
            exc_info=True,
        )


def install_celery_propagation() -> None:
    """幂等注册 Celery 信号；重复调用不会重复挂载。"""
    task_prerun.connect(
        on_task_prerun,
        weak=False,
        dispatch_uid="edgepilot.obs.task_prerun",
    )
    task_postrun.connect(
        on_task_postrun,
        weak=False,
        dispatch_uid="edgepilot.obs.task_postrun",
    )


install_celery_propagation()


# ------------------------------------------------------------------ 线程池


def bind_context(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> Callable[[], T]:
    """捕获**当前** context 并返回可提交给线程池的零参包装。

    用法：``executor.submit(bind_context(worker, arg))``。必须在调用线程
    （持有 trace 的线程）构造；同一个包装对象只能串行进入一次。
    """
    ctx = contextvars.copy_context()

    @functools.wraps(func)
    def _run() -> T:
        return ctx.run(func, *args, **kwargs)

    return _run


def submit_with_context(
    executor: Any, func: Callable[..., T], /, *args: Any, **kwargs: Any
):
    """在调用线程复制 context 后提交任务，保证 worker 线程看到同一 trace。"""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, func, *args, **kwargs)


__all__ = [
    "TRACE_KWARG",
    "RemoteSpan",
    "RemoteTrace",
    "TraceContext",
    "TraceParent",
    "add_traceparent_to_payload",
    "bind_context",
    "current_traceparent",
    "format_traceparent",
    "install_celery_propagation",
    "is_valid_span_id",
    "is_valid_trace_id",
    "on_task_postrun",
    "on_task_prerun",
    "parse_traceparent",
    "submit_with_context",
    "trace_context_from_current",
    "trace_payload_from_current",
]
