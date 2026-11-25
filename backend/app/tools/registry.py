"""Tool Registry：权限 / 校验 / 超时 / 重试 / 埋点 / 审计的统一外壳。

固定执行顺序（见 MVP 设计 §8.2）：::

    查 enabled → 查 agent 绑定 → 校验 permission_scope → 校验 input_schema
    → 超时包装 → 重试策略 → 执行 local handler
    → 校验 output_schema → 写 trace 的 tool_calls → 写 audit（有副作用的工具）

任何一步失败都抛 :class:`~app.tools.executor.ToolExecutionError`（结构化错误码），
绝不向调用方抛裸异常。
"""

import asyncio
import inspect
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.observability.metrics import tool_call_error_total, tool_call_total
from app.observability.records import ToolCallRecord
from app.tools.executor import ToolExecutionError
from app.tools.schema import validate as validate_schema

logger = logging.getLogger(__name__)

# permission_scope 的宽松度排序：Agent 侧权限不得高于工具自身要求
_SCOPE_RANK = {"read": 0, "write": 1, "approval_required": 2}
_DENIED_SCOPES = {"denied", "forbidden", "blocked"}


@dataclass
class ToolContext:
    """一次工具调用的上下文。

    Attributes:
        agent_id: 发起调用的 Agent；None 表示非 Agent 直接调用。
        agent_tool_ids: 该 Agent 被允许的工具 id 集合；``None`` 表示"无绑定信息"，
            跳过 allowlist 检查。``set()`` 表示有绑定但一个都没允许。
        tenant_id: 租户；供内置工具过滤数据，缺省时回退到 contextvar。
        session: 调用方持有的 ``AsyncSession``（可选，优先使用）。
        session_factory: 需要自建会话时使用的工厂（可选）。
        permission_scope: Agent 级权限上限；None 表示不额外收紧。
        trace_id: 关联的 trace；缺省时审计服务回退到 request_id。
        idempotency_key: 幂等键（工作流节点为 ``run:node``）；None 表示不声明。
    """

    agent_id: int | None = None
    agent_tool_ids: set[str] | None = None
    tenant_id: uuid.UUID | None = None
    session: Any | None = None
    session_factory: Any | None = None
    permission_scope: str | None = None
    trace_id: str | None = None
    idempotency_key: str | None = None


@dataclass
class ToolResult:
    """工具执行成功的结果；失败一律走 ``ToolExecutionError``。"""

    ok: bool
    data: Any = None
    error_code: str | None = None
    attempts: int = 1
    error: dict | None = None


@dataclass
class ToolSpec:
    """注册表内部保存的工具定义。"""

    tool_id: str
    version: str
    input_schema: dict | None
    output_schema: dict | None
    handler: Callable[..., Any]
    permission_scope: str = "read"
    timeout_ms: int = 10000
    max_retries: int = 2
    side_effect: bool = False
    dry_run_supported: bool = False
    name: str = ""
    description: str | None = None
    source: str = "local"
    enabled: bool = True
    extra: dict = field(default_factory=dict)

    def public_dict(self) -> dict:
        """API / prompt 工具定义使用的只读视图（不含 handler）。"""
        return {
            "tool_id": self.tool_id,
            "name": self.name or self.tool_id,
            "description": self.description,
            "version": self.version,
            "source": self.source,
            "permission_scope": self.permission_scope,
            "timeout_ms": self.timeout_ms,
            "max_retries": self.max_retries,
            "side_effect": self.side_effect,
            "dry_run_supported": self.dry_run_supported,
            "enabled": self.enabled,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }


def _positional_arity(handler: Callable[..., Any]) -> int:
    """handler 接受的位置参数个数；无法解析时按 1 处理。"""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return 1
    count = 0
    for param in sig.parameters.values():
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            count += 1
        elif param.kind is param.VAR_POSITIONAL:
            break
    return count


class ToolRegistry:
    """进程内工具注册表与执行外壳。

    Args:
        retry_base_delay: 指数退避基数（秒），默认 1s，即 1/4/16s；
            测试可传 0 保持快速。
    """

    def __init__(self, *, retry_base_delay: float = 1.0) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self.retry_base_delay = retry_base_delay

    # ------------------------------------------------------------------ 注册

    def register(
        self,
        tool_id: str,
        version: str,
        input_schema: dict | None,
        output_schema: dict | None,
        handler: Callable[..., Any],
        *,
        permission_scope: str = "read",
        timeout_ms: int = 10000,
        max_retries: int = 2,
        side_effect: bool = False,
        dry_run_supported: bool = False,
        name: str | None = None,
        description: str | None = None,
        source: str = "local",
        enabled: bool = True,
    ) -> ToolSpec:
        """注册/覆盖一个工具。handler 可为同步或异步，签名 ``(args)`` 或 ``(args, ctx)``。"""
        if not tool_id:
            raise ValueError("tool_id 不能为空")
        if max_retries < 0:
            raise ValueError("max_retries 不能为负")
        if timeout_ms <= 0:
            raise ValueError("timeout_ms 必须为正")
        spec = ToolSpec(
            tool_id=tool_id,
            version=version,
            input_schema=input_schema,
            output_schema=output_schema,
            handler=handler,
            permission_scope=permission_scope,
            timeout_ms=timeout_ms,
            max_retries=max_retries,
            side_effect=side_effect,
            dry_run_supported=dry_run_supported,
            name=name or tool_id,
            description=description or _docstring_summary(handler),
            source=source,
            enabled=enabled,
        )
        self._specs[tool_id] = spec
        return spec

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        """启用/禁用工具；不存在时抛 ``tool_not_found``。"""
        spec = self._specs.get(tool_id)
        if spec is None:
            raise ToolExecutionError("tool_not_found")
        spec.enabled = enabled

    def get(self, tool_id: str) -> ToolSpec | None:
        return self._specs.get(tool_id)

    def list_tools(self) -> list[ToolSpec]:
        return [self._specs[key] for key in sorted(self._specs)]

    def __contains__(self, tool_id: object) -> bool:
        return tool_id in self._specs

    def __len__(self) -> int:
        return len(self._specs)

    # ------------------------------------------------------------------ 执行

    async def execute(
        self,
        tool_id: str,
        args: dict | None = None,
        ctx: ToolContext | None = None,
    ) -> ToolResult:
        """按固定流水线执行工具；失败抛 :class:`ToolExecutionError`。"""
        spec = self._specs.get(tool_id)
        try:
            result = await self._execute_inner(tool_id, args or {}, ctx, spec)
        except ToolExecutionError as exc:
            _inc_metric(tool_call_total, tool_id, "failed")
            _inc_metric(tool_call_error_total, tool_id, exc.code)
            raise
        _inc_metric(tool_call_total, tool_id, "succeeded")
        return result

    async def _execute_inner(
        self,
        tool_id: str,
        args: dict,
        ctx: ToolContext | None,
        spec: ToolSpec | None,
    ) -> ToolResult:
        started = time.perf_counter()
        attempts = 0
        async with _open_tool_span(tool_id, spec.version if spec else None) as span:
            _safe(span.set_input, args)
            try:
                if spec is None:
                    raise ToolExecutionError("tool_not_found")
                self._check_enabled(spec)
                self._check_permissions(spec, ctx)
                self._check_input(spec, args)
                data, attempts = await self._invoke_with_retry(spec, args, ctx)
                self._check_output(spec, data)
            except ToolExecutionError as exc:
                _safe(
                    span.record_tool_call,
                    _tool_record(spec, tool_id, args, None, "failed", attempts,
                                 _elapsed_ms(started), exc),
                )
                _safe(span.set_error, exc.code, exc.message, retryable=exc.retryable)
                raise
            except Exception as exc:  # noqa: BLE001 - 外壳自身异常也要结构化返回
                logger.exception("tool %s unexpected failure", tool_id)
                wrapped = ToolExecutionError(
                    "execution_failed",
                    f"工具执行异常：{type(exc).__name__}",
                    retryable=True,
                )
                _safe(
                    span.record_tool_call,
                    _tool_record(spec, tool_id, args, None, "failed", attempts,
                                 _elapsed_ms(started), wrapped),
                )
                _safe(span.set_error, wrapped.code, wrapped.message, retryable=True)
                raise wrapped from exc

            result = ToolResult(ok=True, data=data, attempts=attempts)
            _safe(span.set_output, data)
            _safe(
                span.record_tool_call,
                _tool_record(spec, tool_id, args, data, "succeeded", attempts,
                             _elapsed_ms(started), None),
            )
            await self._maybe_audit(spec, ctx, args, result)
            return result

    # -------------------------------------------------------------- 流水线步骤

    @staticmethod
    def _check_enabled(spec: ToolSpec) -> None:
        if not spec.enabled:
            raise ToolExecutionError("tool_disabled")

    @staticmethod
    def _check_permissions(spec: ToolSpec, ctx: ToolContext | None) -> None:
        # 1) Agent 绑定 allowlist：ctx=None 或 agent_tool_ids=None 视为"无绑定"
        if ctx is not None and ctx.agent_tool_ids is not None:
            if spec.tool_id not in ctx.agent_tool_ids:
                raise ToolExecutionError("permission_denied", "该 Agent 未绑定此工具")

        # 2) 工具自身 permission_scope：本期审批类工具一律拒绝
        scope = spec.permission_scope or "read"
        if scope == "approval_required":
            raise ToolExecutionError("approval_required")
        if scope in _DENIED_SCOPES:
            raise ToolExecutionError("permission_denied")

        # 3) Agent 级权限上限（可选）：不得调用比自身权限更宽的工具
        if ctx is not None and ctx.permission_scope is not None:
            allowed_rank = _SCOPE_RANK.get(ctx.permission_scope)
            if allowed_rank is not None and _SCOPE_RANK.get(scope, 0) > allowed_rank:
                raise ToolExecutionError("permission_denied", "超出 Agent 权限范围")

    @staticmethod
    def _check_input(spec: ToolSpec, args: dict) -> None:
        errors = validate_schema(args, spec.input_schema)
        if errors:
            raise ToolExecutionError(
                "input_schema_invalid",
                f"工具 {spec.tool_id} 入参校验失败",
                detail={"errors": errors[:20]},
            )

    @staticmethod
    def _check_output(spec: ToolSpec, data: Any) -> None:
        errors = validate_schema(data, spec.output_schema)
        if errors:
            raise ToolExecutionError(
                "output_schema_invalid",
                f"工具 {spec.tool_id} 输出校验失败",
                detail={"errors": errors[:20]},
            )

    async def _invoke_with_retry(
        self,
        spec: ToolSpec,
        args: dict,
        ctx: ToolContext | None,
    ) -> tuple[Any, int]:
        """按 max_retries 重试，退避 1/4/16s（以 retry_base_delay 为基数）。"""
        attempts = 0
        last_exc: Exception | None = None
        while attempts <= spec.max_retries:
            attempts += 1
            try:
                return await self._call_handler(spec, args, ctx), attempts
            except Exception as exc:  # noqa: BLE001 - 统一收敛为结构化错误
                last_exc = exc
                if attempts <= spec.max_retries:
                    delay = self.retry_base_delay * (4 ** (attempts - 1))
                    if delay > 0:
                        await asyncio.sleep(delay)
        if last_exc is None:  # 理论上不可达：保证至少执行一次
            raise ToolExecutionError("execution_failed", "工具未执行", retryable=True)
        if isinstance(last_exc, ToolExecutionError):
            raise last_exc
        if isinstance(last_exc, TimeoutError):
            raise ToolExecutionError("timeout", retryable=True) from last_exc
        raise ToolExecutionError(
            "execution_failed",
            f"工具执行失败：{type(last_exc).__name__}: {last_exc}",
            retryable=True,
        ) from last_exc

    async def _call_handler(
        self,
        spec: ToolSpec,
        args: dict,
        ctx: ToolContext | None,
    ) -> Any:
        """调用 handler：按签名决定传 ``(args)`` 还是 ``(args, ctx)``，同步/异步皆可。"""
        arity = _positional_arity(spec.handler)
        call_args = (args, ctx) if arity >= 2 else (args,)

        async def _invoke() -> Any:
            value = spec.handler(*call_args)
            if inspect.isawaitable(value):
                value = await value
            return value

        if spec.timeout_ms and spec.timeout_ms > 0:
            return await asyncio.wait_for(_invoke(), timeout=spec.timeout_ms / 1000)
        return await _invoke()

    # ------------------------------------------------------------------ 审计

    async def _maybe_audit(
        self,
        spec: ToolSpec,
        ctx: ToolContext | None,
        args: dict,
        result: ToolResult,
    ) -> None:
        """仅对 ``side_effect=True`` 的工具写审计；任何失败都只记日志。"""
        if not spec.side_effect:
            return
        try:
            from app.core.tenancy import (
                current_tenant_id_or_none,
                reset_tenant_id,
                set_tenant_id,
            )
            from app.services.audit_service import AuditService

            if ctx is None:
                return
            tenant_id = ctx.tenant_id or current_tenant_id_or_none()
            if tenant_id is None:
                logger.debug("tool audit skipped: no tenant (tool=%s)", spec.tool_id)
                return

            token = None
            if current_tenant_id_or_none() != tenant_id:
                token = set_tenant_id(tenant_id)
            detail = {
                "tool_id": spec.tool_id,
                "version": spec.version,
                "arguments": args,
                "attempts": result.attempts,
                "ok": result.ok,
            }
            try:
                if ctx.session is not None:
                    await AuditService.write(
                        ctx.session,
                        action="tool.call",
                        resource_type="tool",
                        detail=detail,
                        trace_id=ctx.trace_id,
                    )
                elif ctx.session_factory is not None:
                    async with ctx.session_factory() as session:
                        await AuditService.write(
                            session,
                            action="tool.call",
                            resource_type="tool",
                            detail=detail,
                            trace_id=ctx.trace_id,
                        )
            finally:
                if token is not None:
                    reset_tenant_id(token)
        except Exception as exc:  # noqa: BLE001 - 审计是旁路能力
            logger.warning("tool audit failed (tool=%s): %s", spec.tool_id, exc)


def _docstring_summary(handler: Callable[..., Any]) -> str | None:
    doc = inspect.getdoc(handler)
    return doc.strip().splitlines()[0] if doc else None


def _open_tool_span(tool_id: str, version: str | None):
    """打开 tool span。

    H3 埋点缝：从 ``app.observability`` 取进程级 tracer；OBS 关闭 / 无 trace 时
    ``start_span`` 返回 NullSpan，路径不变。观测初始化本身失败也不能影响工具调用。
    """
    try:
        from app.observability import get_tracer

        return get_tracer().start_span(
            f"tool.{tool_id}", "tool", tool_id=tool_id, version=version
        )
    except Exception as exc:  # noqa: BLE001 - 观测是旁路能力
        logger.debug("trace span open failed: %s", exc)
        from app.observability.span import NullSpan

        return NullSpan()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """埋点调用失败绝不影响工具执行。"""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 观测是旁路能力
        logger.debug("observability call failed: %s", exc)


def _inc_metric(metric: Any, *labels: str) -> None:
    try:
        metric.labels(*labels).inc()
    except Exception as exc:  # noqa: BLE001 - 指标是旁路能力
        logger.debug("metric increment failed: %s", exc)


def _tool_record(
    spec: ToolSpec | None,
    tool_id: str,
    args: dict,
    data: Any,
    status: str,
    attempts: int,
    duration_ms: float,
    error: ToolExecutionError | None,
) -> ToolCallRecord:
    permission_result = "granted"
    if error is not None:
        if error.code == "permission_denied":
            permission_result = "denied"
        elif error.code == "approval_required":
            permission_result = "approval_required"
    return ToolCallRecord(
        tool_id=tool_id,
        tool_name=spec.name if spec else tool_id,
        tool_version=spec.version if spec else "unknown",
        description=spec.description if spec else None,
        input_schema=spec.input_schema if spec else None,
        output_schema=spec.output_schema if spec else None,
        input_arguments=args,
        output_result=data,
        execution_status=status,
        permission_result=permission_result,
        duration_ms=duration_ms,
        retry_count=max(attempts - 1, 0),
        error=error.to_dict() if error is not None else None,
    )


# ---------------------------------------------------------------- 默认注册表

_default_registry: ToolRegistry | None = None


def get_default_registry() -> ToolRegistry:
    """进程级默认注册表：首次访问时注册全部内置工具。"""
    global _default_registry
    if _default_registry is None:
        from app.tools.builtin import register_builtins

        registry = ToolRegistry()
        register_builtins(registry)
        _default_registry = registry
    return _default_registry


def reset_default_registry() -> None:
    """测试用：丢弃默认注册表，下次访问重新构建。"""
    global _default_registry
    _default_registry = None
