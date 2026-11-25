"""LLM 统一客户端：``real`` / ``mock`` / ``auto`` 三模式。

职责边界（见 MVP 设计 §8.4）：

* **real**：走 OpenAI 兼容协议（DeepSeek 同协议），只传 ``messages`` 等调用参数；
* **mock**：按 ``fixture_name`` 或 prompt hash 读取 ``tests/fixtures/llm/*.json``，
  未命中返回空内容 + ``mock_fixture_missing``，**绝不伪造内容**；
* **auto**：有 key 时先走 real，无 key 或连续 2 次超时后固定降级为 mock，
  并在响应上标记 ``degraded``。

旁路能力（指标 / 埋点）一律 ``try/except`` 包裹：失败只记 debug，不影响调用。
本模块不记录明文密钥，也不在 INFO 级别记录 prompt 内容。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from app.core.config import get_settings
from app.observability.metrics import (
    llm_request_duration_seconds,
    llm_request_total,
    llm_token_total,
)
from app.observability.records import LLMCallRecord
from app.tools.schema import validate as validate_schema

logger = logging.getLogger(__name__)

_MODES = frozenset({"real", "mock", "auto"})
_FIXTURE_MISSING = "mock_fixture_missing"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")


@dataclass
class LLMResponse:
    """一次 LLM 调用的统一结果。

    ``usage`` 只回填 provider 实际给出的值；拿不到就是 ``None``，不估算。
    ``cost`` 在缺少任一必要单价或 usage 时同样为 ``None``。
    """

    content: str = ""
    structured: dict | None = None
    usage: dict | None = None
    finish_reason: str | None = None
    provider: str = ""
    model_name: str = ""
    request_id: str | None = None
    cost: Decimal | None = None
    error_code: str | None = None
    degraded: bool = False


@dataclass
class ProviderRawResponse:
    """real 路径的 provider 原始结果，仅用于内部解析与埋点。

    独立成 dataclass 便于单测 monkeypatch ``_call_model``，避免触碰网络。
    """

    content: str = ""
    finish_reason: str | None = None
    usage: dict | None = None
    request_id: str | None = None
    raw: dict | None = None


def _canonical_prompt_hash(messages: list[dict]) -> str:
    """``messages`` 的规范 JSON 的 SHA-256 前 16 位十六进制。"""
    try:
        payload = json.dumps(
            messages, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError):
        # 非 JSON 可序列化输入也能得到稳定 key；正常调用不会走到这里。
        payload = repr(messages)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cfg_get(config: Any, key: str) -> Any:
    """同时支持 ORM 对象与 dict 形式的 ModelConfig。"""
    if config is None:
        return None
    if isinstance(config, dict):
        return config.get(key)
    return getattr(config, key, None)


def _as_int(value: Any) -> int | None:
    """只接受真正的整数（排除 bool），其余视为缺失。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _normalize_usage(usage: Any) -> dict | None:
    """把 provider / fixture 的 usage 收敛为已知字段；无有效字段返回 None。"""
    if not isinstance(usage, dict):
        return None
    normalized = {key: _as_int(usage.get(key)) for key in _USAGE_KEYS}
    normalized = {key: value for key, value in normalized.items() if value is not None}
    return normalized or None


def _usage_from_provider(usage: Any) -> dict | None:
    """从 openai SDK 的 usage 对象（或 dict）提取 token 字段，不估算。"""
    if usage is None:
        return None
    values = {
        key: _as_int(_cfg_get(usage, key))
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
    }
    cached = _as_int(_cfg_get(_cfg_get(usage, "prompt_tokens_details"), "cached_tokens"))
    if cached is not None:
        values["cached_tokens"] = cached
    values = {key: value for key, value in values.items() if value is not None}
    return values or None


def _as_decimal(value: Any) -> Decimal | None:
    """把价格字段转成 Decimal；非法值视为缺失。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _compute_cost(usage: dict | None, model_config: Any) -> Decimal | None:
    """按三档单价计算成本；任一必要单价缺失则返回 None，绝不估算。"""
    if model_config is None or not usage:
        return None
    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    if prompt_tokens is None or completion_tokens is None:
        return None

    input_price = _as_decimal(_cfg_get(model_config, "input_price_per_1k"))
    output_price = _as_decimal(_cfg_get(model_config, "output_price_per_1k"))
    if input_price is None or output_price is None:
        return None

    cost = (
        Decimal(prompt_tokens) / Decimal(1000) * input_price
        + Decimal(completion_tokens) / Decimal(1000) * output_price
    )

    cached_tokens = _as_int(usage.get("cached_tokens"))
    if cached_tokens:
        cached_price = _as_decimal(_cfg_get(model_config, "cached_price_per_1k"))
        if cached_price is None:
            # 有缓存 token 但缺缓存单价：无法准确计价，宁可为 None。
            return None
        cost += Decimal(cached_tokens) / Decimal(1000) * cached_price
    return cost


def _error_response(
    code: str,
    provider: str,
    model_name: str,
    *,
    degraded: bool = False,
) -> LLMResponse:
    """构造结构化错误响应：内容为空，绝不编造。"""
    return LLMResponse(
        content="",
        structured=None,
        usage=None,
        finish_reason=None,
        provider=provider,
        model_name=model_name,
        request_id=None,
        cost=None,
        error_code=code,
        degraded=degraded,
    )


class LLMClient:
    """统一 LLM 客户端。

    Args:
        mode: ``real`` / ``mock`` / ``auto``；缺省读 ``get_settings().llm_mode``。
        fixture_dir: mock fixture 目录，默认相对 backend 工作目录。
        fixture_name: 指定 fixture 时优先于 prompt hash。
        api_key: 显式密钥，优先级最高。
        base_url: OpenAI 兼容端点；缺省时回退 ``model_config.base_url``。
        timeout_s: 单次调用超时（秒），由 ``asyncio.wait_for`` 强制。
        prompt_hash_fn: 自定义 prompt hash，便于测试固定命中某个 fixture。
    """

    def __init__(
        self,
        mode: str | None = None,
        *,
        fixture_dir: str = "tests/fixtures/llm",
        fixture_name: str | None = None,
        api_key: str = "",
        base_url: str | None = None,
        timeout_s: float = 20.0,
        prompt_hash_fn: Callable[[list[dict]], str] | None = None,
    ) -> None:
        configured = mode if mode is not None else get_settings().llm_mode
        configured = str(configured or "auto").strip().lower()
        if configured not in _MODES:
            raise ValueError(f"未知 LLM 模式：{configured}")
        if timeout_s <= 0:
            raise ValueError("timeout_s 必须为正")

        self.mode = configured
        self.fixture_dir = fixture_dir
        self.fixture_name = fixture_name
        self.base_url = base_url
        self.timeout_s = timeout_s
        self.prompt_hash_fn = prompt_hash_fn

        self._explicit_api_key = api_key or ""
        self._client: Any | None = None
        self._client_key: tuple[str, str | None] | None = None
        # auto 降级状态：连续超时计数与永久降级标记
        self._timeout_streak = 0
        self._degraded = False
        # 是否见过可用 key：显式参数 / model_config / settings 任一即可
        self._key_available = bool(
            self._explicit_api_key or get_settings().deepseek_api_key
        )

    # ------------------------------------------------------------------ 状态

    @property
    def effective_mode(self) -> str:
        """实际生效的模式：``real`` 或 ``mock``。"""
        if self.mode != "auto":
            return self.mode
        if self._degraded or not self._key_available:
            return "mock"
        return "real"

    # ------------------------------------------------------------------ 调用

    async def chat(
        self,
        messages: list[dict],
        *,
        response_schema: dict | None = None,
        temperature: float = 0.0,
        tools: list[dict] | None = None,
        model_config: Any | None = None,
    ) -> LLMResponse:
        """执行一次对话；任何失败都以 ``LLMResponse.error_code`` 返回。"""
        # auto 模式在选路前先看 model_config 是否带密钥（如 Task 11/12 直接传 ORM 行），
        # 否则会误判为"无 key"而直接走 mock。
        if self.mode == "auto" and not self._key_available and _cfg_get(model_config, "api_key_enc"):
            self._key_available = True
        provider, model_name = self._identity(model_config)
        started = time.perf_counter()
        usage: dict | None = None
        try:
            if self.effective_mode == "mock":
                resp, raw = self._mock_response(
                    messages,
                    response_schema=response_schema,
                    provider=provider,
                    model_name=model_name,
                    model_config=model_config,
                )
            else:
                resp, raw = await self._real_response(
                    messages,
                    response_schema=response_schema,
                    temperature=temperature,
                    tools=tools,
                    model_config=model_config,
                    provider=provider,
                    model_name=model_name,
                )
            usage = resp.usage
            if resp.error_code in (None, "output_schema_invalid"):
                self._record_span(
                    resp,
                    raw,
                    messages,
                    response_schema=response_schema,
                    temperature=temperature,
                    tools=tools,
                )
            return resp
        finally:
            self._record_metrics(provider, model_name, usage, time.perf_counter() - started)

    # ------------------------------------------------------------------ mock

    def _mock_response(
        self,
        messages: list[dict],
        *,
        response_schema: dict | None,
        provider: str,
        model_name: str,
        model_config: Any | None,
    ) -> tuple[LLMResponse, dict | None]:
        """读取 fixture；未命中返回空内容与 ``mock_fixture_missing``。"""
        payload = self._load_fixture(messages)
        degraded = self.mode == "auto"
        if payload is None:
            logger.debug(
                "mock fixture 未命中（fixture_name=%s, fixture_dir=%s）",
                self.fixture_name,
                self.fixture_dir,
            )
            return _error_response(
                _FIXTURE_MISSING, provider, model_name, degraded=degraded
            ), None

        content = payload.get("content")
        if not isinstance(content, str):
            logger.debug("mock fixture 缺少字符串 content，按未命中处理")
            return _error_response(
                _FIXTURE_MISSING, provider, model_name, degraded=degraded
            ), None

        finish_reason = payload.get("finish_reason")
        finish_reason = finish_reason if isinstance(finish_reason, str) else "stop"
        usage = _normalize_usage(payload.get("usage"))
        structured, schema_error = self._finalize_structured(
            payload.get("structured"), content, response_schema
        )
        return LLMResponse(
            content=content,
            structured=structured,
            usage=usage,
            finish_reason=finish_reason,
            provider=provider,
            model_name=model_name,
            request_id=None,
            cost=_compute_cost(usage, model_config),
            error_code=schema_error,
            degraded=degraded,
        ), payload

    def _load_fixture(self, messages: list[dict]) -> dict | None:
        """定位并解析 fixture；目录缺失 / JSON 损坏都视为未命中。"""
        path = self._fixture_path(messages)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("mock fixture 读取失败（%s）：%s", path.name, exc)
            return None
        if not isinstance(data, dict):
            logger.debug("mock fixture 顶层不是对象，按未命中处理（%s）", path.name)
            return None
        return data

    def _fixture_path(self, messages: list[dict]) -> Path:
        if self.fixture_name:
            name = self.fixture_name
            if name.endswith(".json"):
                name = name[: -len(".json")]
            return Path(self.fixture_dir) / f"{name}.json"
        hash_fn = self.prompt_hash_fn or _canonical_prompt_hash
        digest = str(hash_fn(messages))
        return Path(self.fixture_dir) / f"{digest}.json"

    # ------------------------------------------------------------------ real

    async def _real_response(
        self,
        messages: list[dict],
        *,
        response_schema: dict | None,
        temperature: float,
        tools: list[dict] | None,
        model_config: Any | None,
        provider: str,
        model_name: str,
    ) -> tuple[LLMResponse, dict | None]:
        """走真实 provider；所有异常都映射为结构化错误码。"""
        api_key = self._resolve_api_key(model_config)
        if not api_key:
            logger.warning("LLM real 模式缺少 API key（provider=%s）", provider)
            return _error_response("missing_api_key", provider, model_name), None

        base_url = self.base_url or _cfg_get(model_config, "base_url")
        try:
            raw = await asyncio.wait_for(
                self._call_model(
                    api_key=api_key,
                    base_url=str(base_url) if base_url else None,
                    model=model_name,
                    messages=messages,
                    temperature=temperature,
                    response_schema=response_schema,
                    tools=tools,
                ),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            self._timeout_streak += 1
            if self.mode == "auto" and self._timeout_streak >= 2:
                self._degraded = True
                logger.warning(
                    "LLM auto 模式连续 %s 次超时，降级为 mock", self._timeout_streak
                )
            return _error_response(
                "timeout", provider, model_name, degraded=self._is_degraded_now()
            ), None
        except Exception as exc:  # noqa: BLE001 - provider 异常统一收敛，不抛裸异常
            logger.warning(
                "LLM provider 调用失败（provider=%s model=%s）：%s",
                provider,
                model_name,
                type(exc).__name__,
            )
            return _error_response("provider_error", provider, model_name), None

        # 拿到了 provider 响应（哪怕输出不合 schema）就重置连续超时计数
        self._timeout_streak = 0

        content = raw.content if isinstance(raw.content, str) else ""
        structured, schema_error = self._finalize_structured(
            None, content, response_schema
        )
        return LLMResponse(
            content=content,
            structured=structured,
            usage=raw.usage,
            finish_reason=raw.finish_reason,
            provider=provider,
            model_name=model_name,
            request_id=raw.request_id,
            cost=_compute_cost(raw.usage, model_config),
            error_code=schema_error,
            degraded=self._is_degraded_now(),
        ), raw.raw

    async def _call_model(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        messages: list[dict],
        temperature: float,
        response_schema: dict | None,
        tools: list[dict] | None,
    ) -> ProviderRawResponse:
        """调用 OpenAI 兼容接口；单测可 monkeypatch 本方法避免触网。"""
        client = self._get_client(api_key, base_url)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_schema:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools

        completion = await client.chat.completions.create(**kwargs)
        choices = getattr(completion, "choices", None) or []
        choice = choices[0] if choices else None
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        request_id = getattr(completion, "_request_id", None) or getattr(
            completion, "id", None
        )
        return ProviderRawResponse(
            content=content if isinstance(content, str) else "",
            finish_reason=getattr(choice, "finish_reason", None),
            usage=_usage_from_provider(getattr(completion, "usage", None)),
            request_id=request_id if isinstance(request_id, str) else None,
            raw=self._dump_completion(completion),
        )

    def _get_client(self, api_key: str, base_url: str | None):
        """按 (key, base_url) 懒构造并复用 AsyncOpenAI。"""
        cache_key = (api_key, base_url)
        if self._client is None or self._client_key != cache_key:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url or None,
                timeout=self.timeout_s,
                # 不隐藏重试：每次 chat 就是一次可计量的请求
                max_retries=0,
            )
            self._client_key = cache_key
        return self._client

    @staticmethod
    def _dump_completion(completion: Any) -> dict | None:
        """把 SDK 响应转成 dict 供埋点 FULL/REDACTED 模式使用。"""
        dump = getattr(completion, "model_dump", None)
        if callable(dump):
            try:
                data = dump()
            except Exception as exc:  # noqa: BLE001 - 序列化失败不影响调用
                logger.debug("LLM 响应序列化失败：%s", exc)
                return None
            return data if isinstance(data, dict) else None
        return None

    def _resolve_api_key(self, model_config: Any | None) -> str:
        """key 优先级：显式参数 → model_config.api_key_enc → settings。"""
        if self._explicit_api_key:
            self._key_available = True
            return self._explicit_api_key

        encrypted = _cfg_get(model_config, "api_key_enc")
        if encrypted:
            try:
                from app.core.crypto import decrypt_key

                key = decrypt_key(str(encrypted))
            except Exception as exc:  # noqa: BLE001 - 解密失败只降级到 settings
                logger.warning("model_config.api_key_enc 解密失败：%s", type(exc).__name__)
                key = ""
            if key:
                self._key_available = True
                return key

        key = get_settings().deepseek_api_key or ""
        if key:
            self._key_available = True
        return key

    # ------------------------------------------------------------------ 通用

    def _identity(self, model_config: Any | None) -> tuple[str, str]:
        """解析 provider / model_name；mock 模式给稳定占位标签。"""
        provider = _cfg_get(model_config, "provider")
        model_name = _cfg_get(model_config, "model_name")
        if not model_name:
            params = _cfg_get(model_config, "params")
            if isinstance(params, dict):
                model_name = params.get("model")
        if not provider:
            provider = "mock" if self.effective_mode == "mock" else "deepseek"
        if not model_name:
            model_name = "mock" if self.effective_mode == "mock" else "deepseek-chat"
        return str(provider), str(model_name)

    @staticmethod
    def _finalize_structured(
        structured: Any,
        content: str,
        response_schema: dict | None,
    ) -> tuple[dict | None, str | None]:
        """按 ``response_schema`` 校验结构化输出；失败时保留原文并标记错误码。"""
        if not response_schema:
            return structured if isinstance(structured, dict) else None, None

        candidate = structured
        if candidate is None and content:
            try:
                candidate = json.loads(content)
            except (TypeError, ValueError):
                return None, "output_schema_invalid"
        if candidate is None:
            return None, "output_schema_invalid"

        errors = validate_schema(candidate, response_schema)
        if errors:
            logger.debug("LLM 输出未通过 response_schema 校验：%s", errors[:5])
            return None, "output_schema_invalid"
        return candidate if isinstance(candidate, dict) else None, None

    def _is_degraded_now(self) -> bool:
        return self.mode == "auto" and self.effective_mode == "mock"

    def _record_metrics(
        self,
        provider: str,
        model_name: str,
        usage: dict | None,
        elapsed_s: float,
    ) -> None:
        """写 Prometheus 指标；任何失败都不影响调用。"""
        try:
            llm_request_total.labels(provider, model_name).inc()
            llm_request_duration_seconds.labels(provider, model_name).observe(
                max(elapsed_s, 0.0)
            )
            total_tokens = _as_int((usage or {}).get("total_tokens"))
            if total_tokens is not None:
                llm_token_total.labels(provider, model_name, "total").inc(total_tokens)
        except Exception as exc:  # noqa: BLE001 - 指标是旁路能力
            logger.debug("LLM 指标写入失败：%s", exc)

    def _record_span(
        self,
        resp: LLMResponse,
        raw: dict | None,
        messages: list[dict],
        *,
        response_schema: dict | None,
        temperature: float,
        tools: list[dict] | None,
    ) -> None:
        """把调用记录挂到调用方已开启的 span；无 span / OBS 关闭时直接跳过。"""
        try:
            from app.observability.context import current_span
            from app.observability.span import NullSpan

            span = current_span.get()
            if span is None or isinstance(span, NullSpan):
                return
            usage = resp.usage or {}
            span.record_llm_call(
                LLMCallRecord(
                    provider=resp.provider,
                    model_name=resp.model_name,
                    temperature=temperature,
                    tool_definitions=tools,
                    request_messages=messages,
                    raw_response=raw,
                    structured_response=(
                        resp.structured if isinstance(resp.structured, dict) else None
                    ),
                    response_format="json_object" if response_schema else None,
                    finish_reason=resp.finish_reason,
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    cached_tokens=usage.get("cached_tokens"),
                    total_tokens=usage.get("total_tokens"),
                    cost=resp.cost,
                    request_id=resp.request_id,
                    error={"code": resp.error_code} if resp.error_code else None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 埋点是旁路能力
            logger.debug("LLM 埋点写入失败：%s", exc)
