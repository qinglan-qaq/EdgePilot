# EdgePilot 平台侧实施计划（P1–P7）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成 v2 架构的业务底座——16 张表、6 Agent 协作、工作流引擎与节点级审批、边缘节点接入、视觉事件与 RAG、前端 10 个页面，使"一条业务请求 → 多 Agent 取证 → 每句话可追溯的报告"真实跑通。

**Architecture:** FastAPI 模块化单体 + Celery worker + Vue3 前端，严格分层 `api → services → domain ← repositories → infrastructure`；所有业务表带 `tenant_id`，由仓储基类强制过滤；业务代码只通过 `app.observability.tracer` 接触观测（观测实现在另一份计划，本计划只在约定缝上调用）。

**Tech Stack:** Python 3.11 · FastAPI 0.136 · Pydantic 2.13 · SQLAlchemy 2.0（asyncpg）· Alembic · PostgreSQL 16 + pgvector · Redis 7 · RabbitMQ 3.13 + Celery · Mosquitto 2（MQTT）· langgraph 1.0.1 · Vue 3 + TypeScript + Vite + Element Plus + Vue Flow + Pinia

**Spec:** `docs/specs/2026-11-25-edgepilot-architecture.md`（v2 架构，本计划的权威依据）与 `docs/specs/2026-11-25-edgepilot-observability-design.md`（观测契约；本计划只消费其 §4 的埋点接口与 §5 的字段变更）

**配套计划:** `docs/plans/2026-11-25-edgepilot-observability-plan.md`（观测侧；迁移 `0002+`、`app/observability/*` 归它所有）

---

## 计划粒度（先读这一段）

| 范围 | 粒度 | 原因 |
|---|---|---|
| **P1 底座**（Task 1–7） | 代码级：每个 Task 有失败测试、实现代码、运行命令、提交 | 可直接执行，无前置依赖 |
| **P2 Agent/工具/模型**（Task 8–12） | 代码级 | 只依赖 P1 的接口 |
| **P3–P7** | 契约级：文件清单、接口签名、失败测试、验收标准已固定；实现代码在执行进入该 Phase 时按 Task 1–12 的同一模板展开 | P3 的 Supervisor 图结构依赖 P2 实测的 runner 签名；提前写死实现会产生与真实代码不符的"假精确" |

**Gate 规则**：每个 Phase 结束时跑 `make verify`（起栈 → 迁移 → pytest 覆盖率 → e2e），不达标不进下一 Phase。进入 P3 前，必须先把 P3 的 4 个 Task 展开成代码级步骤并在计划文件里补齐（这是硬性动作，不是可选建议）。

## 交接点（与观测计划）

| # | 交接内容 | 谁先 | 平台侧如何应对 |
|---|---|---|---|
| H1 | `app.observability.tracer` 的 facade（`start_trace/start_span/SpanHandle`）+ 进程级 `get_tracer()` | 观测计划 Task 1 | 平台首次 import 是 Task 9（ToolRegistry）；`OBS_ENABLED=false` 时 `get_tracer()` 返回 `enabled=False` 的实例，全部平台测试在该模式下通过。观测 P2 之前 writer 为 `NullWriter`（只丢记录，不报错） |
| H2 | `0001_core` → `0002_obs_base` 的 revision 链 | 平台 Task 5 | 平台只创建 `0001_core`，禁止创建 `0002` 及以后 |
| H3 | `record_llm_call` / `record_tool_call` 的数据类 | 观测计划 Task 1 | 平台 Task 9/10 直接构造这些 data class 并传给 span 句柄 |
| H4 | Agent span 的父子关系校验 | 平台 P3 | 平台 P3 的 agent runner 只需 `tracer.start_span(span_type="agent")`，不关心存储 |
| H5 | 前端壳（路由/布局/HTTP 客户端） | 平台 P6 | 观测计划的观测页面挂在平台创建的 `web/` 壳内 |

## Global Constraints

- **Python 3.11**，依赖只在 `F:\Anaconda_env\lawApp_LangGraph` 可用；系统 PATH 上的 python 不可用（无 fastapi/langgraph，pydantic 1.10）。所有命令在 `conda activate lawApp_LangGraph` 后执行。
- 版本下限（实测值，不得下调）：`langgraph==1.0.1`、`langchain-core==1.6.3`、`fastapi==0.136.1`、`pydantic==2.13.5`、`sqlalchemy==2.0.49`、`openai==2.36.0`、`prometheus-client==0.25.0`、`pgvector==0.5.0`。
- 需新增安装：`alembic`、`celery`、`redis`、`structlog`、`paho-mqtt`、`pydantic-settings`、`argon2-cffi`、`PyJWT`、`cryptography`、`httpx`（已有）、`pytest-asyncio`、`asyncpg`、`psycopg[binary]`、`pgvector`（已有）。
- **所有业务表**：`id bigserial PK`、`tenant_id uuid not null`、`created_at/updated_at timestamptz`（`audit_logs`、`knowledge_chunks` 只有 `created_at`）。唯一约束一律 `(tenant_id, …)`。
- **越权语义**：跨租户访问资源返回 **404**（不是 403，避免泄露存在性）；同租户内角色不足返回 403。
- **响应封套**：`{"code":0,"message":"ok","data":…,"trace_id":"…"}`；响应头 `X-Request-Id` 与 `trace_id` 一致；分页 `?page=1&size=20` → `{"items":[],"total":0,"page":1,"size":20}`。
- **错误码**：400 参数 / 401 未认证 / 403 越权 / 404 不存在 / 409 冲突 / 422 校验 / 429 限流 / 500。
- 写接口支持 `Idempotency-Key`（Redis `SET NX`，TTL 24h，同键同响应）。
- **import 方向**：`domain` 不 import fastapi/sqlalchemy/celery；`api` 不含业务分支；`repositories` 不含业务判断。
- 密钥只从 env 读；`model_configs.api_key_enc` 用 Fernet 加密，任何响应与日志不得出现明文。
- 业务代码**不得**直接写 `traces/spans/llm_calls/tool_calls` 等观测表；只能调用 `app.observability.tracer`。
- 覆盖率门槛：核心模块（`workflow` `tools` `agents` `core.security` `vision.rules`）≥70%，整体 ≥50%。
- 命令唯一入口 `Makefile`：`make up / migrate / test / e2e / verify / seed / demo`。
- 提交粒度：每个 Task 一次提交，信息用 `feat|test|chore|refactor(scope): 描述`。

---

## File Structure（本计划创建/修改的文件）

```
EdgePilot/
├─ Makefile                                   # up/migrate/test/e2e/verify/seed/demo
├─ .env.example
├─ docker/
│  ├─ api.Dockerfile  worker.Dockerfile  web.Dockerfile  edge-agent.Dockerfile  mcp.Dockerfile
│  └─ compose/{docker-compose.yml, docker-compose.demo.yml, .env.example}
├─ backend/
│  ├─ pyproject.toml  requirements.txt  alembic.ini
│  ├─ migrations/{env.py, versions/0001_core.py}
│  ├─ app/
│  │  ├─ main.py  worker.py
│  │  ├─ core/{config.py, logging.py, exceptions.py, middleware.py, tenancy.py,
│  │  │        security.py, pagination.py, rate_limit.py, metrics.py, idempotency.py}
│  │  ├─ api/{deps.py, v1/{router.py, auth.py, users.py, models.py, tools.py, agents.py,
│  │  │        workflows.py, runs.py, nodes.py, devices.py, events.py, knowledge.py,
│  │  │        dashboard.py, audit.py, ws.py}}
│  │  ├─ schemas/{common.py, auth.py, agent.py, model.py, tool.py, workflow.py, run.py,
│  │  │        device.py, event.py, knowledge.py, dashboard.py}
│  │  ├─ domain/{enums.py, errors.py, entities/, policies/{permissions.py, approval.py, retention.py}}
│  │  ├─ models/{base.py, user.py, agent.py, model_config.py, tool.py, workflow.py, run.py,
│  │  │        node.py, device.py, event.py, knowledge.py, audit.py}
│  │  ├─ repositories/{base.py, user_repo.py, agent_repo.py, workflow_repo.py, run_repo.py,
│  │  │        node_repo.py, device_repo.py, event_repo.py, knowledge_repo.py, audit_repo.py}
│  │  ├─ services/{auth_service.py, agent_service.py, tool_service.py, model_service.py,
│  │  │        workflow_service.py, run_service.py, node_service.py, device_service.py,
│  │  │        event_service.py, knowledge_service.py, dashboard_service.py, audit_service.py}
│  │  ├─ workflow/{dsl.py, validator.py, engine.py, executor.py, expression.py,
│  │  │        nodes/{start.py, agent_node.py, tool_node.py, condition.py, approval_node.py, end.py}}
│  │  ├─ agents/{base.py, supervisor.py, device.py, vision.py, operations.py, diagnosis.py,
│  │  │        report.py, schemas.py, prompts/*.md, llm.py}
│  │  ├─ tools/{registry.py, executor.py, builtin/*.py, notify/, mcp/client.py}
│  │  ├─ rag/{loader.py, chunker.py, embedder.py, indexer.py, retriever.py, reranker.py, service.py}
│  │  ├─ vision/{detectors/, rules/, snapshot.py}
│  │  ├─ messaging/{celery_app.py, mqtt/{client.py, topics.py, handlers.py}, pubsub.py}
│  │  └─ tasks/{workflow_tasks.py, ingest_tasks.py, notify_tasks.py, retention_tasks.py}
│  └─ tests/{conftest.py, unit/, integration/, e2e/, perf/, fixtures/}
├─ edge-agent/edge_agent/{main.py, config.py, mqtt_client.py, registry.py, heartbeat.py,
│              devices/, pipeline/, outbox.py, simulator.py}
├─ mcp-server/mcp_server/{main.py, auth.py, tools/}
├─ scripts/{seed_demo.py, seed_prompts.py, retention.py, offline_pack.sh, wait_for_stack.sh}
└─ web/src/{main.ts, App.vue, api/, stores/, router/, layouts/, views/, components/, composables/, types/, styles/}
```

---

## P1 底座

### Task 1: 后端脚手架、配置与健康检查

**Files:**
- Create: `backend/pyproject.toml`、`backend/requirements.txt`、`backend/.env.example`、`backend/app/__init__.py`、`backend/app/core/config.py`、`backend/app/core/logging.py`、`backend/app/main.py`、`backend/tests/conftest.py`
- Test: `backend/tests/unit/core/test_settings.py`、`backend/tests/unit/core/test_health.py`

**Interfaces:**
- Produces: `app.core.config.Settings`（字段见下）、`app.core.config.get_settings() -> Settings`、`app.main.create_app() -> FastAPI`、`app.main.app`
- Consumes: 无。本 Task 不 import `app.observability`，观测 facade 首次使用在 Task 9

- [ ] **Step 1: 写失败测试（配置）**

```python
# backend/tests/unit/core/test_settings.py
from app.core.config import Settings


def test_settings_have_safe_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.env == "dev"
    assert s.tenant_default_id == "00000000-0000-0000-0000-000000000001"
    assert s.obs_enabled is False
    assert s.obs_capture_mode == "REDACTED"
    assert s.obs_queue_size == 10000
    assert s.obs_batch_size == 200
    assert s.access_token_minutes == 15
    assert s.refresh_token_days == 7


def test_database_url_comes_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    s = Settings(_env_file=None)
    assert s.database_url.endswith("/db")
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && pytest tests/unit/core/test_settings.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.core.config'`

- [ ] **Step 3: 实现配置与日志**

```toml
# backend/pyproject.toml
[project]
name = "edgepilot-backend"
version = "0.1.0"
requires-python = ">=3.11"

[tool.pytest.ini_options]
asyncio_mode = "auto"          # 异步 fixture 不必逐个加 pytest_asyncio.fixture
testpaths = ["tests"]
```

```text
# backend/requirements.txt（实测下限固定，其余装最新兼容版）
fastapi==0.136.1
pydantic==2.13.5
sqlalchemy==2.0.49
prometheus-client==0.25.0
openai==2.36.0
langgraph==1.0.1
langchain-core==1.6.3
pgvector==0.5.0
uvicorn[standard]
alembic
asyncpg
psycopg[binary]
celery
redis
structlog
paho-mqtt
pydantic-settings
argon2-cffi
PyJWT
cryptography
httpx
pytest
pytest-asyncio
```

```python
# backend/app/core/config.py
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "edgepilot"
    env: str = "dev"
    database_url: str = "postgresql+asyncpg://edgepilot:edgepilot@localhost:5432/edgepilot"
    test_database_url: str = "postgresql+asyncpg://edgepilot:edgepilot@localhost:5432/edgepilot_test"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://edgepilot:edgepilot@localhost:5672//"
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""

    jwt_secret: str = "dev-only-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    tenant_default_id: str = "00000000-0000-0000-0000-000000000001"

    obs_enabled: bool = False
    obs_capture_mode: str = "REDACTED"  # OFF | METADATA | REDACTED | FULL
    obs_queue_size: int = 10000
    obs_batch_size: int = 200
    obs_flush_ms: int = 500
    obs_sample_rate: float = 1.0
    obs_live_ttl_seconds: int = 3600

    llm_mode: str = "auto"  # real | mock | auto
    llm_fixture_dir: str = "tests/fixtures/llm"
    deepseek_api_key: str = ""
    storage_dir: str = "./var"
    snapshot_ttl_days: int = 90
    metrics_enabled: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# backend/app/core/logging.py
import logging
import sys

import structlog


def configure_logging(settings) -> None:
    level = logging.DEBUG if settings.env == "dev" else logging.INFO
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


logger = structlog.get_logger()
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && pytest tests/unit/core/test_settings.py -v`
Expected: 2 passed

- [ ] **Step 5: 写健康检查测试**

```python
# backend/tests/unit/core/test_health.py
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.mark.asyncio
async def test_health_returns_ok():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "x-request-id" in {k.lower() for k in resp.headers}
```

- [ ] **Step 6: 运行，确认失败**

Run: `cd backend && pytest tests/unit/core/test_health.py -v`
Expected: FAIL（`ModuleNotFoundError: app.main`）

- [ ] **Step 7: 实现应用入口与请求 ID 中间件**

```python
# backend/app/core/middleware.py
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-Id"] = rid
        return response
```

```python
# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import RequestIdMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="EdgePilot API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "env": settings.env}

    return app


app = create_app()
```

- [ ] **Step 8: 运行，确认通过并提交**

Run: `cd backend && pytest tests/unit -v`
Expected: 3 passed

```bash
git add backend/pyproject.toml backend/requirements.txt backend/.env.example backend/app backend/tests
git commit -m "feat(backend): 脚手架、配置、结构化日志与健康检查"
```

---

### Task 2: 统一响应、异常、分页与 Request-Id 透出

**Files:**
- Create: `backend/app/core/exceptions.py`、`backend/app/schemas/common.py`、`backend/app/core/pagination.py`、`backend/app/api/__init__.py`、`backend/app/api/v1/__init__.py`、`backend/app/api/v1/router.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/unit/core/test_response_envelope.py`

**Interfaces:**
- Produces: `AppError(code, http_status, message)` 及子类 `Unauthorized/Forbidden/NotFound/Conflict/RateLimited`；`ok(data, trace_id=None)`；`ApiResponse[T]`；`PageParams(page, size)`、`PageResult[T](items, total)`
- 供后续所有 API 使用

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/core/test_response_envelope.py
import pytest
from httpx import ASGITransport, AsyncClient

from app.core.exceptions import NotFound
from app.main import create_app


@pytest.fixture
def app():
    app = create_app()

    @app.get("/_boom")
    async def boom():
        raise NotFound("工作流不存在")

    @app.get("/_ok")
    async def ok_endpoint():
        from app.schemas.common import ok

        return ok({"a": 1})

    return app


@pytest.mark.asyncio
async def test_success_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/_ok")
    body = r.json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert body["data"] == {"a": 1}
    assert body["trace_id"] == r.headers["X-Request-Id"]


@pytest.mark.asyncio
async def test_error_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/_boom")
    assert r.status_code == 404
    body = r.json()
    assert body["code"] == 404
    assert body["message"] == "工作流不存在"
    assert body["data"] is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && pytest tests/unit/core/test_response_envelope.py -v`
Expected: FAIL（缺少 `app.core.exceptions`）

- [ ] **Step 3: 实现异常、封套与分页**

```python
# backend/app/core/exceptions.py
class AppError(Exception):
    code = 400
    http_status = 400

    def __init__(self, message: str | None = None, *, detail: dict | None = None) -> None:
        self.message = message or "bad request"
        self.detail = detail
        super().__init__(self.message)


class Unauthorized(AppError):
    code = http_status = 401


class Forbidden(AppError):
    code = http_status = 403


class NotFound(AppError):
    code = http_status = 404


class Conflict(AppError):
    code = http_status = 409


class RateLimited(AppError):
    code = http_status = 429
```

```python
# backend/app/schemas/common.py
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

from app.core.middleware import request_id_var

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = 0
    message: str = "ok"
    data: T | None = None
    trace_id: str | None = None


def ok(data=None) -> dict:
    return {"code": 0, "message": "ok", "data": data, "trace_id": request_id_var.get()}


class PageParams(BaseModel):
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int
```

- [ ] **Step 4: 注册异常处理器并挂路由**

```python
# backend/app/api/v1/router.py
from fastapi import APIRouter

api_router = APIRouter(prefix="/api/v1")


@api_router.get("/ping")
async def ping() -> dict:
    return {"pong": True}
```

```python
# 追加到 backend/app/main.py 的 create_app 内
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.exceptions import AppError
from app.core.middleware import request_id_var


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message, "data": None,
                     "trace_id": request_id_var.get()},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"code": 422, "message": "参数校验失败", "data": exc.errors(),
                     "trace_id": request_id_var.get()},
        )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
```

在 `create_app()` 中 `app.include_router(api_router)` 与 `register_exception_handlers(app)` 各加一行。

- [ ] **Step 5: 运行，确认通过并提交**

Run: `cd backend && pytest tests/unit/core -v`
Expected: 5 passed

```bash
git add backend/app backend/tests
git commit -m "feat(backend): 统一响应封套、异常处理与分页"
```

---

### Task 3: 租户上下文与仓储基类

**Files:**
- Create: `backend/app/core/tenancy.py`、`backend/app/models/base.py`、`backend/app/models/__init__.py`、`backend/app/repositories/base.py`、`backend/app/db.py`
- Test: `backend/tests/unit/core/test_tenancy.py`、`backend/tests/integration/repositories/test_tenant_filter.py`

**Interfaces:**
- Produces: `set_tenant_id(uuid) -> Token`、`reset_tenant_id(token)`、`get_tenant_id() -> UUID`、`current_tenant_id_or_none()`；`Base`、`TenantMixin(tenant_id)`、`TimestampMixin(created_at, updated_at)`；`BaseRepository(session)` 及其 `select(model)`（自动 `where tenant_id == get_tenant_id()`）
- Consumes: 无

- [ ] **Step 1: 写失败测试（纯单元，不需要 DB）**

```python
# backend/tests/unit/core/test_tenancy.py
import uuid

import pytest

from app.core.tenancy import (
    TenantNotSetError,
    current_tenant_id_or_none,
    get_tenant_id,
    reset_tenant_id,
    set_tenant_id,
)


def test_get_tenant_id_raises_when_unset():
    with pytest.raises(TenantNotSetError):
        get_tenant_id()


def test_set_and_reset_tenant():
    tid = uuid.uuid4()
    token = set_tenant_id(tid)
    try:
        assert get_tenant_id() == tid
        assert current_tenant_id_or_none() == tid
    finally:
        reset_tenant_id(token)
    assert current_tenant_id_or_none() is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && pytest tests/unit/core/test_tenancy.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现租户上下文与模型基类**

```python
# backend/app/core/tenancy.py
import uuid
from contextvars import ContextVar, Token


class TenantNotSetError(RuntimeError):
    """请求链路缺少租户上下文；宁可报错也不能漏过滤。"""


_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("tenant_id", default=None)


def set_tenant_id(value: uuid.UUID) -> Token:
    return _tenant_id.set(value)


def reset_tenant_id(token: Token) -> None:
    _tenant_id.reset(token)


def current_tenant_id_or_none() -> uuid.UUID | None:
    return _tenant_id.get()


def get_tenant_id() -> uuid.UUID:
    value = _tenant_id.get()
    if value is None:
        raise TenantNotSetError("tenant_id 未设置：请求链路缺少租户上下文")
    return value
```

```python
# backend/app/models/base.py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


class TenantMixin:
    @declared_attr
    def tenant_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(UUID(as_uuid=True), nullable=False, index=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

```python
# backend/app/repositories/base.py
from typing import Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import get_tenant_id
from app.models.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def select(self, *where) -> Select:
        stmt = select(self.model).where(self.model.tenant_id == get_tenant_id())
        if where:
            stmt = stmt.where(*where)
        return stmt

    async def get_or_404(self, pk: int) -> ModelT:
        from app.core.exceptions import NotFound

        obj = await self.session.scalar(self.select(self.model.id == pk))
        if obj is None:
            raise NotFound()
        return obj

    async def count(self, *where) -> int:
        stmt = select(func.count()).select_from(self.model).where(
            self.model.tenant_id == get_tenant_id()
        )
        if where:
            stmt = stmt.where(*where)
        return await self.session.scalar(stmt) or 0
```

- [ ] **Step 4: 实现数据库会话入口**

```python
# backend/app/db.py
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session
```

`conftest.py` 的 DB/客户端夹具统一放到 Task 4 一次性补齐（那时 `User` 模型已存在，可直接播种登录用户）。本 Task 只有单元测试，不需要数据库。

- [ ] **Step 5: 运行单元测试并提交**

Run: `cd backend && pytest tests/unit/core/test_tenancy.py -v`
Expected: 2 passed

```bash
git add backend/app/core/tenancy.py backend/app/models backend/app/repositories backend/app/db.py backend/tests
git commit -m "feat(backend): 租户上下文、强制过滤仓储基类与会话入口"
```

---

### Task 4: 认证、JWT 与 RBAC

**Files:**
- Create: `backend/app/models/user.py`、`backend/app/core/security.py`、`backend/app/services/auth_service.py`、`backend/app/schemas/auth.py`、`backend/app/api/deps.py`、`backend/app/api/v1/auth.py`、`backend/app/repositories/user_repo.py`
- Modify: `backend/app/api/v1/router.py`、`backend/tests/conftest.py`
- Test: `backend/tests/unit/core/test_security.py`、`backend/tests/integration/test_auth_flow.py`

**Interfaces:**
- Produces: `hash_password/verify_password`、`create_token(sub, tenant_id, role, kind) -> str`、`decode_token(token) -> dict`；`get_current_user() -> User`（依赖）、`require_role(*roles)`。
- 角色枚举：`admin | operator | viewer`。

- [ ] **Step 1: 写失败测试（哈希与令牌）**

```python
# backend/tests/unit/core/test_security.py
import pytest

from app.core.security import create_token, decode_token, hash_password, verify_password


def test_password_roundtrip():
    h = hash_password("s3cret-pass")
    assert h != "s3cret-pass"
    assert verify_password(h, "s3cret-pass") is True
    assert verify_password(h, "wrong") is False


def test_token_roundtrip_and_type_claim():
    token = create_token(sub="1", tenant_id="t-1", role="admin", kind="access")
    payload = decode_token(token)
    assert payload["sub"] == "1"
    assert payload["tid"] == "t-1"
    assert payload["role"] == "admin"
    assert payload["typ"] == "access"


def test_expired_token_rejected():
    from app.schemas.auth import TokenPayload
    from app.core.security import encode_payload
    from datetime import datetime, timedelta, timezone

    token = encode_payload(
        TokenPayload(sub="1", tid="t-1", role="viewer", typ="access",
                     iat=datetime(2020, 1, 1, tzinfo=timezone.utc),
                     exp=datetime(2020, 1, 1, 0, 1, tzinfo=timezone.utc))
    )
    with pytest.raises(Exception):
        decode_token(token)
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd backend && pytest tests/unit/core/test_security.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现安全模块**

```python
# backend/app/schemas/auth.py
from datetime import datetime

from pydantic import BaseModel


class TokenPayload(BaseModel):
    sub: str
    tid: str
    role: str
    typ: str  # access | refresh
    iat: datetime
    exp: datetime
    jti: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
```

```python
# backend/app/core/security.py
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.schemas.auth import TokenPayload

_ph = PasswordHasher()


def hash_password(raw: str) -> str:
    return _ph.hash(raw)


def verify_password(hashed: str, raw: str) -> bool:
    try:
        return _ph.verify(hashed, raw)
    except VerifyMismatchError:
        return False


def encode_payload(payload: TokenPayload) -> str:
    s = get_settings()
    return jwt.encode(payload.model_dump(), s.jwt_secret, algorithm=s.jwt_algorithm)


def create_token(sub: str, tenant_id: str, role: str, kind: str) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    ttl = timedelta(minutes=s.access_token_minutes) if kind == "access" else timedelta(days=s.refresh_token_days)
    return encode_payload(TokenPayload(sub=sub, tid=tenant_id, role=role, typ=kind,
                                       iat=now, exp=now + ttl, jti=secrets.token_hex(8)))


def decode_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd backend && pytest tests/unit/core/test_security.py -v`
Expected: 3 passed

- [ ] **Step 5: 实现用户模型、登录/刷新/登出/me 与 RBAC 依赖**

`users` 表字段按 v2 §2：`email`、`password_hash`、`display_name`、`role`、`status`；索引 `unique(tenant_id, email)`。

```python
# backend/app/api/deps.py
import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import decode_token
from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.db import get_db
from app.models.user import User

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise Unauthorized("缺少令牌")
    payload = decode_token(creds.credentials)
    if payload.get("typ") != "access":
        raise Unauthorized("令牌类型错误")
    token = set_tenant_id(uuid.UUID(payload["tid"]))
    try:
        user = await session.get(User, int(payload["sub"]))
        if user is None or user.status != "active":
            raise Unauthorized("用户不可用")
        return user
    except Exception:
        reset_tenant_id(token)
        raise


def require_role(*roles: str):
    async def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise Forbidden("角色不足")
        return user

    return _dep
```

`auth.py` 端点：`POST /auth/login`（校验密码，返回 TokenPair，写 `audit_logs`）、`POST /auth/refresh`（校验 `typ=refresh`）、`POST /auth/logout`（`SETEX deny:{jti} 900 1`）、`GET /auth/me`。

- [ ] **Step 6: 写集成测试（登录、刷新、越权、跨租户 404）并运行**

```python
# backend/tests/integration/test_auth_flow.py
import pytest

from tests.conftest import auth_headers, login_token


@pytest.mark.asyncio
async def test_login_and_me(client, seeded_users):
    r = await client.post("/api/v1/auth/login",
                          json={"email": "admin@edgepilot.local", "password": "Passw0rd!"})
    body = r.json()
    assert body["code"] == 0
    token = body["data"]["access_token"]

    r2 = await client.get("/api/v1/auth/me", headers=auth_headers(token))
    assert r2.json()["data"]["role"] == "admin"


@pytest.mark.asyncio
async def test_viewer_cannot_publish_agent(client, seeded_users):
    token = await login_token(client, "viewer@edgepilot.local")
    r = await client.post("/api/v1/agents/1/publish", headers=auth_headers(token))
    assert r.status_code == 403
    assert r.json()["code"] == 403


@pytest.mark.asyncio
async def test_cross_tenant_returns_404(client, seeded_users):
    token = await login_token(client, "other_tenant_admin@edgepilot.local")
    r = await client.get("/api/v1/agents/1", headers=auth_headers(token))
    assert r.status_code == 404
```

```python
# backend/tests/conftest.py（Task 4 追加）
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import get_db
from app.main import create_app
from app.models.user import User

PASSWORD = "Passw0rd!"
ADMIN_EMAIL = "admin@edgepilot.local"
VIEWER_EMAIL = "viewer@edgepilot.local"
OTHER_ADMIN_EMAIL = "other_tenant_admin@edgepilot.local"
TENANT = uuid.UUID(get_settings().tenant_default_id)
OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """测试库建表一次。alembic/env.py 读 DATABASE_URL，所以临时指向 test 库。"""
    from alembic import command
    from alembic.config import Config

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = get_settings().test_database_url
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old


@pytest.fixture
async def db_conn(migrated_db):
    """所有 session 都绑到同一个连接 + 外层事务，测试结束整体回滚。"""
    engine = create_async_engine(get_settings().test_database_url, poolclass=NullPool)
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            yield conn
        finally:
            await tx.rollback()
    await engine.dispose()


@pytest.fixture
async def session(db_conn):
    async with async_sessionmaker(bind=db_conn, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
async def session_factory(db_conn):
    return async_sessionmaker(bind=db_conn, expire_on_commit=False)


@pytest.fixture
async def client(session):
    app = create_app()

    async def _override_get_db():
        yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def login_token(client, email: str, password: str = PASSWORD) -> str:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return r.json()["data"]["access_token"]


@pytest.fixture
async def seeded_users(session):
    users = [
        User(tenant_id=TENANT, email=ADMIN_EMAIL, password_hash=hash_password(PASSWORD),
             display_name="Admin", role="admin", status="active"),
        User(tenant_id=TENANT, email=VIEWER_EMAIL, password_hash=hash_password(PASSWORD),
             display_name="Viewer", role="viewer", status="active"),
        User(tenant_id=OTHER_TENANT, email=OTHER_ADMIN_EMAIL,
             password_hash=hash_password(PASSWORD), display_name="Other", role="admin",
             status="active"),
    ]
    session.add_all(users)
    await session.flush()
    return users


@pytest.fixture
async def admin_token(client, seeded_users):
    return await login_token(client, ADMIN_EMAIL)


@pytest.fixture
async def viewer_token(client, seeded_users):
    return await login_token(client, VIEWER_EMAIL)


@pytest.fixture
async def other_tenant_token(client, seeded_users):
    """另一个租户的 admin：用来验证跨租户访问返回 404 而非 403。"""
    return await login_token(client, OTHER_ADMIN_EMAIL)
```

上面 `session_factory` 与 `session` 绑同一个连接，是为了让 `BatchWriter`（观测 P2）在独立 session 里写的数据既对断言可见、又能随外层事务一起回滚。`asyncio_mode = "auto"` 已让 `@pytest.fixture` 支持 async 函数。

- [ ] **Step 7: 提交**

```bash
git add backend/app backend/tests
git commit -m "feat(backend): JWT 认证、argon2 口令与 RBAC 依赖"
```

---

### Task 5: `0001_core` 迁移（16 张业务表 + §5.1 字段变更）

**Files:**
- Create: `backend/alembic.ini`、`backend/migrations/env.py`、`backend/migrations/versions/0001_core.py`、`backend/app/models/{agent,model_config,tool,workflow,run,node,device,event,knowledge,audit}.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/integration/test_schema_manifest.py`

**Interfaces:**
- Produces: 16 张表；ORM 类 `User/Agent/AgentVersion/ModelConfig/Tool/AgentToolBinding/Workflow/WorkflowVersion/WorkflowRun/WorkflowNodeRun/EdgeNode/Device/VisionEvent/AgentRun/KnowledgeChunk/AuditLog`
- 字段权威来源：v2 稿 §2 的 ER 表；本 Task 只**增删**下列字段：

| 表 | 增 | 删 |
|---|---|---|
| `workflow_runs` | `root_span_id varchar(16) null` | — |
| `workflow_node_runs` | `span_id varchar(16) null` | — |
| `agent_runs` | `span_id varchar(16) null`、`total_tokens int null`、`total_cost numeric(12,6) null` | `token_usage jsonb`、`tool_calls jsonb` |
| `agents` | `prompt_template_id bigint null`、`prompt_version int null` | `prompt_system text` |
| `tools` | `version varchar(32) not null default '1.0.0'`、`dry_run_supported bool not null default false` | — |
| `model_configs` | `input_price_per_1k/ output_price_per_1k/ cached_price_per_1k numeric(10,6) null` | — |

- [ ] **Step 1: 写失败测试（schema 清单断言）**

```python
# backend/tests/integration/test_schema_manifest.py
import pytest
from sqlalchemy import text

EXPECTED_TABLES = {
    "users", "agents", "agent_versions", "model_configs", "tools", "agent_tool_bindings",
    "workflows", "workflow_versions", "workflow_runs", "workflow_node_runs",
    "edge_nodes", "devices", "vision_events", "agent_runs", "knowledge_chunks", "audit_logs",
}
EXPECTED_COLUMNS = {
    "workflow_runs": {"root_span_id"},
    "workflow_node_runs": {"span_id"},
    "agent_runs": {"span_id", "total_tokens", "total_cost"},
    "agents": {"prompt_template_id", "prompt_version"},
    "tools": {"version", "dry_run_supported"},
    "model_configs": {"input_price_per_1k", "output_price_per_1k", "cached_price_per_1k"},
}
REMOVED_COLUMNS = {
    "agents": {"prompt_system"},
    "agent_runs": {"token_usage", "tool_calls"},
}
TENANT_TABLES = {"users", "agents", "agent_versions", "model_configs", "tools",
                 "agent_tool_bindings", "workflows", "workflow_versions", "workflow_runs",
                 "workflow_node_runs", "edge_nodes", "devices", "vision_events",
                 "agent_runs", "knowledge_chunks", "audit_logs"}


@pytest.mark.asyncio
async def test_all_business_tables_exist(session):
    rows = await session.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ))
    assert EXPECTED_TABLES <= {r[0] for r in rows}


@pytest.mark.asyncio
async def test_added_and_removed_columns(session):
    for table, columns in EXPECTED_COLUMNS.items():
        rows = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": table})
        actual = {r[0] for r in rows}
        assert columns <= actual, f"{table} 缺少 {columns - actual}"
    for table, columns in REMOVED_COLUMNS.items():
        rows = await session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"
        ), {"t": table})
        assert not (columns & {r[0] for r in rows}), f"{table} 仍存在 {columns}"


@pytest.mark.asyncio
async def test_every_business_table_is_tenant_scoped(session):
    for table in TENANT_TABLES:
        rows = await session.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name='tenant_id'"
        ), {"t": table})
        row = rows.first()
        assert row is not None, f"{table} 缺少 tenant_id"
        assert row[0] == "NO", f"{table}.tenant_id 必须 NOT NULL"
```

- [ ] **Step 2: 运行，确认失败**

Run: `make up && make migrate`（此步迁移尚不存在，`alembic upgrade head` 无 revision）→ `pytest tests/integration/test_schema_manifest.py -v`
Expected: FAIL（表不存在）

- [ ] **Step 3: 实现 ORM 模型与 `0001_core`**

按 v2 §2 逐表实现 ORM（`mapped_column` 风格，`TenantMixin` + `TimestampMixin`），并按上表增删字段。迁移文件关键片段：

```python
# backend/migrations/versions/0001_core.py
"""0001_core: 16 张业务表（含观测前置字段）"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def _tenant():
    return sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _tenant(),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    op.create_table(
        "agents",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _tenant(),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("agent_type", sa.String(32), nullable=False),
        sa.Column("model_config_id", sa.BigInteger),
        sa.Column("prompt_template_id", sa.BigInteger),
        sa.Column("prompt_version", sa.Integer),
        sa.Column("config", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("current_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_agents_tenant_name"),
    )
    op.create_index("ix_agents_tenant_type", "agents", ["tenant_id", "agent_type"])

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        _tenant(),
        sa.Column("workflow_run_id", sa.BigInteger),
        sa.Column("node_run_id", sa.BigInteger),
        sa.Column("agent_id", sa.BigInteger, nullable=False),
        sa.Column("agent_version", sa.Integer),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input", postgresql.JSONB),
        sa.Column("output", postgresql.JSONB),
        sa.Column("span_id", sa.String(16)),
        sa.Column("total_tokens", sa.Integer),
        sa.Column("total_cost", sa.Numeric(12, 6)),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("trace_id", sa.String(32)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_agent_runs_agent_created", "agent_runs", ["agent_id", "created_at"])

    # 其余 13 张表按 v2 §2 字段与索引逐个 create_table；
    # 唯一约束一律带 tenant_id，顺序：model_configs → tools → agent_versions →
    # agent_tool_bindings → workflows → workflow_versions → workflow_runs →
    # workflow_node_runs → edge_nodes → devices → vision_events → knowledge_chunks → audit_logs


def downgrade() -> None:
    for table in ("audit_logs", "knowledge_chunks", "vision_events", "devices",
                  "edge_nodes", "workflow_node_runs", "workflow_runs", "workflow_versions",
                  "workflows", "agent_tool_bindings", "agent_versions", "tools",
                  "model_configs", "agent_runs", "agents", "users"):
        op.drop_table(table)
```

同时按 v2 §2「关键索引与约束」表补齐：`workflow_node_runs` 的 `(run_id, node_id)` 唯一 + `status='waiting_approval'` 部分索引；`vision_events.dedup_key` 唯一 + `(tenant_id, occurred_at desc)`；`knowledge_chunks` 的 HNSW 向量索引与 `to_tsvector('simple', content)` GIN；`audit_logs` 两个索引。

- [ ] **Step 4: 写租户强制过滤的集成测试**

```python
# backend/tests/integration/repositories/test_tenant_filter.py
import uuid

import pytest

from app.core.tenancy import reset_tenant_id, set_tenant_id
from app.models.user import User
from app.repositories.user_repo import UserRepo


@pytest.mark.asyncio
async def test_repository_filters_by_tenant(session):
    tid_a, tid_b = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        User(tenant_id=tid_a, email="a@x.com", password_hash="h", display_name="A",
             role="admin", status="active"),
        User(tenant_id=tid_b, email="b@x.com", password_hash="h", display_name="B",
             role="admin", status="active"),
    ])
    await session.commit()

    token = set_tenant_id(tid_a)
    try:
        repo = UserRepo(session)
        assert await repo.count() == 1
        assert (await repo.get_by_email("b@x.com")) is None
    finally:
        reset_tenant_id(token)
```

- [ ] **Step 5: 运行，确认通过**

Run: `make migrate && cd backend && pytest tests/integration -v`
Expected: schema 清单 3 passed；租户过滤 1 passed

- [ ] **Step 6: 迁移可回滚验证**

Run: `cd backend && alembic downgrade base && alembic upgrade head`
Expected: 两条命令均成功，无残留表

- [ ] **Step 7: 提交**

```bash
git add backend/alembic.ini backend/migrations backend/app/models backend/tests
git commit -m "feat(db): 0001_core 迁移，16 张业务表与观测前置字段"
```

---

### Task 6: 指标、审计与限流

**Files:**
- Create: `backend/app/core/metrics.py`、`backend/app/services/audit_service.py`、`backend/app/core/rate_limit.py`、`backend/app/api/v1/audit.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/unit/core/test_metrics_endpoint.py`

**Interfaces:**
- Produces: `metrics.http_request_total/http_request_duration/audit_write_total`、`render() -> (bytes, str)`；`AuditService.write(session, *, action, resource_type, resource_id, detail, user) -> AuditLog`；`limit(key, times=10, seconds=60) -> None`（超限抛 `RateLimited`）
- 指标名固定：`http_request_total{method,path,status}`、`http_request_duration_seconds`、`audit_write_total{action,result}`；`path` 必须用路由模板（`request.scope["route"].path`），取不到时用 `"unmatched"`，禁止用原始 URL（高基数）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/core/test_metrics_endpoint.py
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.mark.asyncio
async def test_metrics_exposes_prometheus_text_with_samples():
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as c:
        await c.get("/health")          # 先产生一个样本，否则带标签的 Counter 不会输出
        r = await c.get("/metrics")
    assert r.status_code == 200
    assert "# HELP http_request_total" in r.text
    assert 'http_request_total{' in r.text
    assert 'status="200"' in r.text
```

- [ ] **Step 2: 运行，确认失败；Step 3: 实现**

```python
# backend/app/core/metrics.py
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

http_request_total = Counter("http_request_total", "HTTP 请求总数", ["method", "path", "status"])
http_request_duration = Histogram("http_request_duration_seconds", "HTTP 请求耗时",
                                  ["method", "path"])
audit_write_total = Counter("audit_write_total", "审计写入总数", ["action", "result"])


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
```

`main.py` 增加 `@app.get("/metrics")`，返回 `Response(content=body, media_type=ct)`；再加一个基于 `BaseHTTPMiddleware` 的计时中间件累加上面两个指标（`path` 用路由模板，避免高基数，取 `request.scope.get("route").path`，取不到时用 `"unmatched"`）。

审计服务：`write(session, *, action, resource_type, resource_id, detail, user) -> AuditLog`，`trace_id` 取当前观测 trace（没有则取 `request_id_var`），写入失败**不得**影响主流程（`try/except` + `audit_write_total.labels(result="failed")`）。

- [ ] **Step 4: 运行并提交**

Run: `cd backend && pytest tests/unit/core -v`
Expected: 全部 passed

```bash
git add backend/app backend/tests
git commit -m "feat(backend): Prometheus 指标、审计写入与令牌桶限流"
```

---

### Task 7: Docker Compose、Makefile 与 CI

**Files:**
- Create: `docker/api.Dockerfile`、`docker/worker.Dockerfile`、`docker/compose/docker-compose.yml`、`docker/compose/.env.example`、`Makefile`、`.github/workflows/ci.yml`、`scripts/wait_for_stack.sh`
- Modify: `backend/tests/conftest.py`（追加 `redis_client` 夹具）
- Test: `backend/tests/integration/test_stack_health.py`

**Interfaces:**
- Produces: `make up/down/migrate/test/e2e/verify`；compose 服务 `postgres/rabbitmq/redis/mosquitto/api/worker`

- [ ] **Step 1: 写 compose 与 Dockerfile**

compose 关键约束：`postgres` 用 `pgvector/pgvector:pg16`，数据卷持久化；`rabbitmq:3.13-management-alpine`；`redis:7-alpine` 带 `healthcheck: redis-cli ping`；`eclipse-mosquitto:2`；`api` 依赖 postgres/redis 的 `condition: service_healthy`，健康检查 `GET /health`；`worker` 复用 api 镜像，命令 `celery -A app.worker worker -Q q.workflow,q.ingest,q.notify -c 2`。`docker/compose/.env.example` 里 `OBS_ENABLED=true`、`OBS_CAPTURE_MODE=REDACTED`、`LLM_MODE=mock`（测试默认仍是 `OBS_ENABLED=false`）。

- [ ] **Step 2: 写失败测试（栈健康）**

```python
# backend/tests/conftest.py（追加）
import redis.asyncio as aioredis
import pytest

from app.core.config import get_settings


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()
```

```python
# backend/tests/integration/test_stack_health.py
import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_database_reachable(session):
    assert await session.scalar(text("SELECT 1")) == 1


@pytest.mark.asyncio
async def test_redis_reachable(redis_client):
    assert await redis_client.ping() is True
```

- [ ] **Step 3: 运行**

Run: `make up && cd backend && pytest tests/integration/test_stack_health.py -v`
Expected: 2 passed

- [ ] **Step 4: 提交**

```bash
git add docker Makefile .github scripts
git commit -m "chore(infra): compose core profile、Makefile 与 CI"
```

---

## P2 Agent / 工具 / 模型

### Task 8: 模型配置（Fernet 加密密钥 + 连通性测试）

**Files:**
- Create: `backend/app/models/model_config.py`、`backend/app/schemas/model.py`、`backend/app/services/model_service.py`、`backend/app/api/v1/models.py`
- Test: `backend/tests/unit/services/test_model_service.py`

**Interfaces:**
- Produces: `ModelService.create/update/list/get`；`encrypt_key(plain) -> str`、`decrypt_key(enc) -> str`；`ModelService.test_connection(id) -> dict`
- 约束：任何响应 schema 不含 `api_key_enc`；未配置单价时三个价格字段为 `null`

- [ ] **Step 1–4（TDD）**: 失败测试断言 `create` 后 DB 里 `api_key_enc` 是密文且 `decrypt_key` 可还原；`ModelOut` 序列化结果不含 `api_key`/`api_key_enc`；`test_connection` 在 `provider=mock` 时返回 `{"ok": True, "latency_ms": 0}`；实现 `ModelService` 与 CRUD 路由（admin 写、viewer 读）。

```python
# 测试要点（实际写在 test_model_service.py）
async def test_api_key_is_encrypted_at_rest(...):
    created = await svc.create(tenant_id, name="ds", model_type="llm", provider="deepseek",
                               model_name="deepseek-chat", api_key="sk-plain")
    row = await session.scalar(select(ModelConfig).where(ModelConfig.id == created.id))
    assert row.api_key_enc != "sk-plain"
    assert decrypt_key(row.api_key_enc) == "sk-plain"
```

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(backend): 模型配置 CRUD 与密钥加密"
```

---

### Task 9: Tool Registry 与工具执行外壳

**Files:**
- Create: `backend/app/models/tool.py`、`backend/app/tools/registry.py`、`backend/app/tools/executor.py`、`backend/app/tools/builtin/*.py`、`backend/app/api/v1/tools.py`
- Test: `backend/tests/unit/tools/test_tool_executor.py`、`backend/tests/unit/tools/test_tool_permissions.py`

**Interfaces:**
- Produces: `ToolRegistry.register(tool_id, version, input_schema, output_schema, handler, *, permission_scope="read", timeout_ms=10000, max_retries=2, side_effect=False, dry_run_supported=False)`；`ToolRegistry.execute(tool_id, args, ctx: ToolContext) -> ToolResult`；`ToolResult(ok, data, error_code, attempts)`
- **埋点缝（H3）**：`execute` 从 `from app.observability import get_tracer` 取进程级 tracer（H1），内部 `async with get_tracer().start_span(f"tool.{tool_id}", "tool", tool_id=tool_id, version=version) as span:`，成功后 `span.record_tool_call(ToolCallRecord(tool_id=tool_id, tool_name=tool_id, tool_version=version, input_arguments=args, output_result=result.data, execution_status="succeeded", retry_count=result.attempts - 1))`，失败 `span.set_error(code, msg, retryable=...)`。`ToolCallRecord` 来自 `app.observability.records`。`OBS_ENABLED=false` 时 `get_tracer()` 返回的实例 `start_span` 直接给 no-op 句柄，这段代码路径不变。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/tools/test_tool_executor.py
import pytest

from app.tools.registry import ToolRegistry, ToolResult
from app.tools.executor import ToolExecutionError


@pytest.mark.asyncio
async def test_schema_validation_returns_422_code():
    reg = ToolRegistry()
    reg.register("echo", "1.0.0",
                 input_schema={"type": "object", "required": ["msg"],
                               "properties": {"msg": {"type": "string"}}},
                 output_schema=None, handler=lambda args: {"ok": args["msg"]})
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("echo", {}, ctx=None)
    assert ei.value.code == "input_schema_invalid"


@pytest.mark.asyncio
async def test_timeout_maps_to_timeout_code():
    import asyncio
    reg = ToolRegistry()
    async def slow(args): await asyncio.sleep(1)
    reg.register("slow", "1.0.0", input_schema=None, output_schema=None,
                 handler=slow, timeout_ms=10, max_retries=0)
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("slow", {}, ctx=None)
    assert ei.value.code == "timeout"


@pytest.mark.asyncio
async def test_retry_records_attempts():
    reg = ToolRegistry()
    calls = {"n": 0}
    async def flaky(args):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("boom")
        return {"ok": True}
    reg.register("flaky", "1.0.0", input_schema=None, output_schema=None,
                 handler=flaky, max_retries=2)
    result = await reg.execute("flaky", {}, ctx=None)
    assert result.ok is True
    assert calls["n"] == 2
```

```python
# backend/tests/unit/tools/test_tool_permissions.py
from app.tools.registry import ToolContext, ToolRegistry
from app.tools.executor import ToolExecutionError


async def test_agent_bound_allowlist_enforced():
    reg = ToolRegistry()
    reg.register("danger", "1.0.0", input_schema=None, output_schema=None,
                 handler=lambda args: {"ok": True}, side_effect=True)
    with pytest.raises(ToolExecutionError) as ei:
        await reg.execute("danger", {}, ctx=ToolContext(agent_tool_ids=set()))
    assert ei.value.code == "permission_denied"
```

- [ ] **Step 2–4**: 实现 `ToolRegistry`（权限 → schema → 超时 `asyncio.wait_for` → 指数退避 1/4/16s → 输出 schema 校验 → 埋点 → 审计）与 9 个 builtin 工具骨架（`query_device_status` 等，P5 前用仓储读真实表、无数据时返回空列表而不是假数据）。**`ctx=None` 表示"无 Agent 绑定"，跳过 allowlist 检查但保留 `permission_scope` 校验**——否则 `test_tool_executor.py` 里三个 `ctx=None` 用例会在权限环节提前失败。每次执行结束 `from app.observability.metrics import tool_call_total, tool_call_error_total`，`tool_call_total.labels(tool_id, "succeeded"|"failed").inc()`，失败时再 `tool_call_error_total.labels(tool_id, code).inc()`。运行确认全部通过。

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(tools): Tool Registry 与权限/校验/超时/重试外壳"
```

---

### Task 10: LLMClient（real/mock/auto 降级 + usage 记录）

**Files:**
- Create: `backend/app/agents/llm.py`、`backend/tests/fixtures/llm/*.json`
- Test: `backend/tests/unit/agents/test_llm_client.py`

**Interfaces:**
- Produces: `LLMClient(mode: str | None = None, *, fixture_dir: str = "tests/fixtures/llm", fixture_name: str | None = None, api_key: str = "", base_url: str | None = None, timeout_s: float = 20.0, prompt_hash_fn: Callable[[list[dict]], str] | None = None)`；`LLMClient.mode`（配置值）、`LLMClient.effective_mode`（实际生效：`real`/`mock`）、`LLMClient.chat(messages, *, response_schema=None, temperature=0.0, tools=None, model_config=None) -> LLMResponse`；`LLMResponse(content, structured, usage, finish_reason, provider, model_name, request_id, cost, error_code=None, degraded=False)`
- **埋点缝（H3）**：进入 `chat` 前由调用方已开 span，`start_span` 会把它写进 `context.current_span`；`chat` 成功后 `from app.observability.context import current_span` 取 `span = current_span.get()`，非空则 `span.record_llm_call(LLMCallRecord(provider=resp.provider, model_name=resp.model_name, request_messages=messages, raw_response=raw, finish_reason=resp.finish_reason, prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"), total_tokens=usage.get("total_tokens"), cost=resp.cost, request_id=resp.request_id, error=resp.error_code and {"code": resp.error_code}))`，为空（`OBS_ENABLED=false` 或调用方没开 span）直接跳过——不能因此报错。`usage` 拿不到时字段为 `None`，`cost` 为 `None`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/unit/agents/test_llm_client.py
import pytest

from app.agents.llm import LLMClient


@pytest.mark.asyncio
async def test_mock_mode_hits_fixture_by_prompt_hash(tmp_path, monkeypatch):
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "abc123.json").write_text('{"content": "固定回答", "finish_reason": "stop"}',
                                         encoding="utf-8")
    client = LLMClient(mode="mock", fixture_dir=str(fixture), prompt_hash_fn=lambda m: "abc123")
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "固定回答"


@pytest.mark.asyncio
async def test_mock_mode_prefers_named_fixture(tmp_path):
    fixture = tmp_path / "llm"
    fixture.mkdir()
    (fixture / "device_ok.json").write_text(
        '{"content": "ok", "finish_reason": "stop", "usage": {"total_tokens": 20}}',
        encoding="utf-8")
    client = LLMClient(mode="mock", fixture_dir=str(fixture), fixture_name="device_ok")
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert resp.usage["total_tokens"] == 20


@pytest.mark.asyncio
async def test_mock_miss_returns_structured_refusal_not_fabrication(tmp_path):
    client = LLMClient(mode="mock", fixture_dir=str(tmp_path / "empty"), prompt_hash_fn=lambda m: "zzz")
    resp = await client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == ""
    assert resp.error_code == "mock_fixture_missing"


@pytest.mark.asyncio
async def test_auto_degrades_after_two_timeouts():
    client = LLMClient(mode="auto", api_key="", timeout_s=0.01)
    assert client.mode == "auto"
    for _ in range(2):
        await client.chat([{"role": "user", "content": "x"}])
    assert client.effective_mode == "mock"


@pytest.mark.asyncio
async def test_cost_is_none_when_price_missing():
    client = LLMClient(mode="mock", fixture_dir="tests/fixtures/llm", prompt_hash_fn=lambda m: "abc123")
    resp = await client.chat([{"role": "user", "content": "hi"}], model_config=None)
    assert resp.cost is None
```

- [ ] **Step 2–4**: 实现三条路径：`real`（`openai.AsyncOpenAI(base_url=...)`，DeepSeek 兼容 OpenAI 协议）、`mock`（`fixture_name` 优先于 hash：给了就读 `{fixture_dir}/{fixture_name}.json`，否则读 `{fixture_dir}/{sha256(canonical_json(messages))[:16]}.json`；文件格式 `{"content": str, "structured": dict|null, "finish_reason": str, "usage": {"prompt_tokens", "completion_tokens", "total_tokens"}}`；未命中时 `content=""`、`error_code="mock_fixture_missing"`，不得编造）、`auto`（无 key 或连续 2 次 `asyncio.TimeoutError` → 切 mock，并在 `LLMResponse.degraded=True` 标注）。成本计算：`cost = prompt_tokens/1000*input_price + completion_tokens/1000*output_price + cached_tokens/1000*cached_price`；任一所需单价为 `None` → `cost=None`。每次请求 `from app.observability.metrics import llm_request_duration_seconds, llm_request_total, llm_token_total`：`llm_request_total.labels(provider, model_name).inc()`、`llm_request_duration_seconds.labels(provider, model_name).observe(elapsed_s)`、有 usage 时 `llm_token_total.labels(provider, model_name, "total").inc(total_tokens)`。

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(agents): LLMClient 三模式与 token/成本口径"
```

---

### Task 11: Agent CRUD、版本发布与 Prompt 引用

**Files:**
- Create: `backend/app/models/agent.py`、`backend/app/schemas/agent.py`、`backend/app/services/agent_service.py`、`backend/app/api/v1/agents.py`、`scripts/seed_prompts.py`
- Test: `backend/tests/integration/test_agent_publish.py`

**Interfaces:**
- Produces: `AgentService.create/update/publish/list/get`；`publish` 在同一事务里插 `agent_versions(version+1, snapshot)` + 更新 `agents.current_version`；`snapshot.prompt = {"template_id": …, "version": …, "rendered_hash": …}`
- 依赖观测计划的 `prompt_templates`（H2 之后）；在此之前 `prompt_template_id` 允许为 `null`，`publish` 只在非空时校验模板版本存在。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integration/test_agent_publish.py
import pytest


@pytest.mark.asyncio
async def test_publish_creates_immutable_version_and_bumps_current(client, admin_token):
    payload = {"name": "vision-a", "agent_type": "vision", "prompt_template_id": None,
               "prompt_version": None, "config": {}}
    created = (await client.post("/api/v1/agents", json=payload,
                                 headers=auth_headers(admin_token))).json()["data"]
    r1 = await client.post(f"/api/v1/agents/{created['id']}/publish",
                           headers=auth_headers(admin_token))
    assert r1.json()["data"]["version"] == 1
    r2 = await client.post(f"/api/v1/agents/{created['id']}/publish",
                           headers=auth_headers(admin_token))
    assert r2.json()["data"]["version"] == 2


@pytest.mark.asyncio
async def test_agent_version_rows_are_not_updatable(client, admin_token):
    # 应用层禁止 UPDATE agent_versions：调用 PATCH 不存在该路由 → 405
    r = await client.patch("/api/v1/agents/1/versions/1", headers=auth_headers(admin_token))
    assert r.status_code in (404, 405)
```

- [ ] **Step 2–4**: 实现 CRUD、发布事务、`GET /agents/{id}/versions`、`GET /agents/{id}/runs`；`scripts/seed_prompts.py` 先把 6 个 `agents/prompts/*.md` 写入 `prompt_templates/prompt_versions`（表由观测计划 `0003` 创建；脚本在表不存在时以 `--check` 模式报错退出，不静默跳过）。

- [ ] **Step 5: 提交**

```bash
git commit -m "feat(agents): Agent CRUD、不可变版本发布与 Prompt 引用字段"
```

---

### Task 12: 单 Agent 试跑（`POST /agents/{id}/test`）

**Files:**
- Create: `backend/app/agents/base.py`、`backend/app/api/v1/agents.py`（追加端点）、`backend/app/repositories/agent_repo.py`
- Modify: `backend/tests/conftest.py`（追加 `llm_fixtures`、`seeded_agent`、`seeded_agent_bad_output`）
- Test: `backend/tests/integration/test_agent_test_run.py`

**Interfaces:**
- Produces: `run_agent(agent: Agent, task: dict, *, tracer, llm: LLMClient) -> AgentRunResult(agent_run_id, output, span_id, trace_id)`；端点 `POST /api/v1/agents/{id}/test` 返回 `{"id", "agent_id", "status", "output", "error", "trace_id", "span_id", "total_tokens", "total_cost"}`，其中 `error` 直接取 `agent_runs.error`（`Text`）的 `f"{code}: {message}"` 文本，无错误时为 `null`。
- **埋点缝（H4）**：`async with tracer.start_trace(f"agent_test.{agent.name}", source="agent_test") as trace:` / `async with tracer.start_span(f"agent.{agent.agent_type}", "agent", agent_id=agent.id, agent_version=agent.current_version) as span:`；结束时把 `span.span_id or None`、`trace.trace_id or None` 写入 `agent_runs.span_id/trace_id`（**空串一律转 `None`**，否则违反 `varchar(16)` 的语义），`total_tokens/total_cost` 由 `LLMResponse` 累加后回写。`OBS_ENABLED=false` 时句柄是 no-op 对象，`.trace_id/.span_id` 为空串，代码路径不变。
- 端点内构造 `LLMClient(mode=settings.llm_mode, fixture_dir=settings.llm_fixture_dir, fixture_name=agent.config.get("mock_fixture"))`，每次请求读一次 `get_settings()`，便于测试用 env 覆盖。
- `tracer` 同样在端点内每次请求取 `get_tracer()`，不要模块级缓存；否则 `OBS_ENABLED` 的测试开关对已 import 的实例无效。

- [ ] **Step 1: 写失败测试（含夹具）**

```python
# backend/tests/conftest.py（追加）
import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "llm"


@pytest.fixture(scope="session", autouse=True)
def llm_fixtures():
    """写入两个确定性 mock 响应，供 integration 测试使用。"""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "device_ok.json").write_text(json.dumps({
        "content": '{"answer": "设备在线", "evidence": [{"type": "metric", "ref": "m1"}]}',
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }, ensure_ascii=False), encoding="utf-8")
    (FIXTURE_DIR / "device_bad.json").write_text(json.dumps({
        "content": '{"answer": 123}',       # 类型错且缺 evidence
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    }, ensure_ascii=False), encoding="utf-8")
    return FIXTURE_DIR


async def _make_published_agent(client, admin_token, name: str, mock_fixture: str) -> int:
    payload = {"name": name, "agent_type": "device",
               "prompt_template_id": None, "prompt_version": None,
               "config": {"system_prompt": "你是设备诊断助手", "mock_fixture": mock_fixture}}
    created = (await client.post("/api/v1/agents", json=payload,
                                 headers=auth_headers(admin_token))).json()["data"]
    await client.post(f"/api/v1/agents/{created['id']}/publish",
                      headers=auth_headers(admin_token))
    return created["id"]


@pytest.fixture
async def seeded_agent(client, admin_token, llm_fixtures):
    return await _make_published_agent(client, admin_token, "device-check", "device_ok")


@pytest.fixture
async def seeded_agent_bad_output(client, admin_token, llm_fixtures):
    return await _make_published_agent(client, admin_token, "device-bad", "device_bad")
```

```python
# backend/tests/integration/test_agent_test_run.py
import pytest

from tests.conftest import FIXTURE_DIR, auth_headers


@pytest.fixture(autouse=True)
def deterministic_obs_and_llm(monkeypatch):
    """每次请求都重新读设置，所以这里改 env 足以同时影响 tracer 与 LLMClient。"""
    import app.observability as obs
    from app.core.config import get_settings

    monkeypatch.setenv("OBS_ENABLED", "true")
    monkeypatch.setenv("LLM_MODE", "mock")
    monkeypatch.setenv("LLM_FIXTURE_DIR", str(FIXTURE_DIR))
    get_settings.cache_clear()
    obs.reset_tracer()
    yield
    obs.reset_tracer()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_test_run_links_agent_run_to_trace(client, admin_token, seeded_agent):
    r = await client.post(f"/api/v1/agents/{seeded_agent}/test",
                          json={"task": {"question": "设备在线吗"}},
                          headers=auth_headers(admin_token))
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["agent_id"] == seeded_agent
    assert data["status"] == "succeeded"
    assert len(data["trace_id"]) == 32
    assert len(data["span_id"]) == 16
    assert data["total_tokens"] == 20


@pytest.mark.asyncio
async def test_schema_invalid_output_marks_failed_after_one_retry(
        client, admin_token, seeded_agent_bad_output):
    r = await client.post(f"/api/v1/agents/{seeded_agent_bad_output}/test",
                          json={"task": {"question": "x"}},
                          headers=auth_headers(admin_token))
    data = r.json()["data"]
    assert data["status"] == "failed"
    assert "agent_output_invalid" in (data["error"] or "")
    # 只重试 1 次：2 条 llm_calls 由观测层记录（P2 完成后断言），此处只保证失败语义
```

- [ ] **Step 2–4**: 实现 `run_agent`：预算检查（token 上限、60s 超时）→ 组装 messages（Prompt 模板由观测计划的 renderer 提供，暂未接入时用 `agents/config` 里的内联 system 作为过渡，并在 `metadata.prompt_source="inline_fallback"` 标注）→ `LLMClient.chat` → Pydantic Schema 校验 / 失败重试 1 次 → 写 `agent_runs`（`span_id`/`trace_id` 空串转 `None`）→ 关闭 span。

- [ ] **Step 5: 运行并提交**

Run: `make migrate && cd backend && pytest tests/integration/test_agent_test_run.py -v`
Expected: 2 passed

```bash
git commit -m "feat(agents): 单 Agent 试跑与 agent_run 落库"
```

---

## P3 多 Agent 编排（契约级，进入前展开为代码级）

| Task | 交付物 | 文件 | 关键接口 / 约束 | 失败测试（进入时补齐实现代码） |
|---|---|---|---|---|
| 13 | 6 个 Agent 的 I/O 契约与结构化输出 | `agents/schemas.py`、`agents/{device,vision,operations,diagnosis,report}.py` | 每类输出 Pydantic 模型；结论必须带 `evidence[]`，空证据的结论字段置 `None` | `test_domain_agent_schema.py`：缺 evidence 的结论被判 `agent_output_invalid` |
| 14 | Supervisor（Plan-and-Execute + `Send` 扇出） | `agents/supervisor.py` | 硬上限：≤6 次 Agent 调用、≤2 次重规划、60k token、60s；无数据工具权限 | `test_supervisor_budget.py`：第 7 次调用被拒并返回部分结果 |
| 15 | 并行取证与合并 | `agents/supervisor.py`（`join/assess` 节点） | 并行分支 `parent_span_id` 相同；超时分支返回 `branch_timeout` 不拖垮整图 | `test_supervisor_parallel.py`：两个并行 agent span 时间区间重叠、父节点唯一 |
| 16 | 报告生成与证据链 | `agents/report.py`、`tools/builtin/report_gen.py` | 每条结论可回溯 `event_id/metric/chunk_id` | `test_report_evidence.py`：报告中每个编号在 `evidence` 中存在 |

**P3 退出验收**：`make e2e` 跑 `tests/e2e/test_agent_flow.py`——一次请求在 `agent_runs` 产生 6 行，且 `traces` 中该 trace 的 agent span 为 6 个（观测计划提供 `GET /traces/{id}/tree` 断言）。

### Task 13 代码级展开（已实现）

**文件**：新增 `app/agents/schemas.py`（模型与校验）、`app/agents/prompts.py`（md prompt 加载 + 消息构造）、`app/agents/registry.py`（类型 → 模块）、六个薄模块 `app/agents/{supervisor,device,vision,operations,diagnosis,report}.py`；改造 `app/agents/base.py` 去掉硬编码设备 schema；测试 `tests/unit/agents/test_domain_agent_schema.py`、`tests/integration/test_agent_domain_run.py`，并在 `tests/conftest.py` 为 `_make_published_agent` 增加 `agent_type` / `config_extra` 可选参数。

**模型**：`Evidence(type, ref, snippet?)`；基类 `EvidenceBackedOutput(evidence=[], CONCLUSION_FIELDS)`；领域模型 `SupervisorOutput(plan, assignments, summary)`、`DeviceOutput(answer, device_status)`、`VisionOutput(summary, events)`、`OperationsOutput(summary, findings)`、`DiagnosisOutput(conclusion, possible_causes)`、`ReportOutput(markdown, evidence)`。

**证据规则**：`model_validator(mode="after")` 仅在「结论字段非空且 `evidence == []`」时抛 `ValueError`；空结论 + 空证据是合法的“不编造”路径。Supervisor / Report 不强制（Report 证据链留给 Task 16）。

**运行语义**：`base._execute` 以 `output_schema_for(agent_type)` 传 `response_schema`、以 `parse_output(agent_type, candidate)` 做 Pydantic 校验；校验失败仍只重试 1 次，第二次失败落 `agent_output_invalid`。预算 / 超时 / `agent_runs` / 观测语义不变。

**测试**：`test_domain_agent_schema.py` 覆盖六类模型的证据约束、`parse_output` 与 `output_schema_for`；`test_agent_domain_run.py` 用 `diagnosis_ok` / `diagnosis_no_evidence` 两个 fixture 验证成功落库与「缺证据 → 一次重试后 `agent_output_invalid`」。

### Task 14 代码级展开（已实现）

**文件**：`app/agents/supervisor.py`（`SupervisorLimits` / `Assignment` / `SupervisorResult` / `Supervisor` + 图节点）、`agents/prompts/supervisor.md`（plan/assignments 输出契约）、`tests/unit/agents/test_supervisor_budget.py`。

**图结构**：`START → plan → [Send("agent", {assignment}) × N →] assess → plan | finalize → END`；并行 `agent` 节点只返回 `{"results": [entry]}`，由 `operator.add` 合并，`errors` 同为 additive reducer。planner / runner 均可注入，单测不触网不碰库。

**预算落点（代码而非 prompt）**：`plan` 按 `max_agent_calls - len(results)` 切片，超出部分丢弃并记 `agent_call_limit: 已丢弃 N 个超出上限的派发`；`assess` 累加已回报 token，超 60k 记 `budget_exceeded` 且禁止再重规划；失败且 `replans < 2` 才回 `plan`；整体 `asyncio.wait_for(timeout_s)`，超时从共享缓冲返回已完成结果并记 `timeout`。

**拒绝语义**：白名单仅 `device/vision/diagnosis/operations/report`，`tool` / `supervisor` 在派发前落 `supervisor_invalid_assignment`，runner 调用数为 0；无 LLM 时默认 planner 返回 `supervisor_planner_unavailable`，绝不编造计划。

**结果状态**：全部派发成功且无告警 → `succeeded`；存在被丢弃/失败/告警但仍有结果 → `partial`；无任何结果 → `failed`。`finalize` 原样保留子输出与 `evidence[]`，`summary` 只做确定性计数。

**测试**：`test_supervisor_budget.py` —— 第 7 次调用被拒并保留 6 条部分结果、重规划止于 2 次（planner 共 3 次）、越权类型零调用、35k×2 触发 `budget_exceeded` 停止重规划。

### Task 15 代码级展开（已实现）

**文件**：`app/agents/supervisor.py`（分支 span + `branch_timeout`）、`app/agents/base.py`（`run_agent` 句柄参数）、`tests/unit/agents/test_supervisor_parallel.py`。

**Span 归属**：`_agent_node` 为每个 `Send` 分支单独开 `agent.{agent_type}`（`span_type="agent"`），输入 `{goal, input}`；因分支各自持有复制上下文，代码在开 span 后**显式**把 `parent_span_id` 指到 `supervisor.run` span（父 id 经图状态 `supervisor_span_id` 传递，不依赖 contextvar 是否被复制），全部子 span 共享同一 `trace_id`。

**分支超时**：`SupervisorLimits.branch_timeout_s`（默认 30s）用 `asyncio.wait_for` 包住单次 runner；超时生成 `status="failed"`、`error` 以 `branch_timeout:` 开头的条目并给 span 标 `branch_timeout`（retryable），其它分支照常完成，图级 `asyncio.wait_for` 仍兜底整体。

**默认 runner 嵌套 trace 修复**：`run_agent` 新增 keyword-only `trace_handle` / `span_handle`；传入时不再新开 trace/agent span，只在既有句柄上写 input/output/error/meta 且不负责收口，运行行仍记录句柄 id。`Supervisor._default_runner` 从 `app.observability.context` 读取当前 trace/span 并传入，默认路径只产生一条 trace（supervisor + 子 agent span）；不传句柄时行为与 Task 12/13 完全一致。

**测试**：`test_supervisor_parallel.py` —— 两个并行 agent span 时间区间重叠且 `parent_span_id` 同为 `supervisor.run`；`branch_timeout_s=0.05` 时慢分支失败、快分支结果保留、整体 `partial` 且不抛异常。

### Task 16 代码级展开（已实现）

**文件**：`app/agents/report.py`（确定性构建器）、`app/agents/schemas.py`（`ReportHighlight` / `ReportOutput.title|highlights`）、`app/tools/builtin/report_gen.py`（由 `builtin/report.py` 重命名，工具 ID 仍为 `generate_report`）、`tests/unit/agents/test_report_evidence.py`。

**构建 API**：`EVIDENCE_TYPES={event_id,metric,chunk_id}`、`ReportEvidenceError(code="report_evidence_invalid")`、`ReportFinding(statement, agent_type, evidence)`、`collect_findings(results)`、`render_report(title, findings)`、`build_report(results, title=?, task=?)`；全程不调用 LLM，只搬运 Supervisor 成功子结果。

**结论提取**：仅 `status=="succeeded"` 且 `output` 为 dict 的条目；device 取 `answer`（空白时回退 `device_status`），vision/operations 取 `summary`，diagnosis 取 `conclusion`，跳过 report/supervisor 与未知类型；空白结论或无证据结论均不写入报告（不补造证据）。

**编号与去重**：证据按首次使用顺序全局编号，`(type, ref)` 相同复用同一编号；同一结论重复证据只引用一次，故 `[n]` 恒为 `1..len(evidence)`，且每条结论的 `highlights[].evidence` 非空。

**严格校验**：`type` 必须命中白名单、`ref` 去空白后非空、`snippet` 可空但必须是字符串；任何非法证据（含缺失字段、非对象项）立即抛 `ReportEvidenceError`，不静默丢弃。

**无证据行为**：无有效结论时 Markdown 固定输出 `证据不足：没有可回溯证据支持的结论。`，`evidence=[]`、`highlights=[]`、`finding_count=0`；标题优先级为显式 `title` → `task["title"]` → `task["goal"]` → `EdgePilot 诊断报告`。返回结构可直接 `ReportOutput.model_validate`（未对 `ReportOutput` 自身开启证据强制，`ReportOutput(markdown=...)` 仍合法）。

**工具兼容**：`generate_report` 的 schema、`permission_scope="read"`、超时/重试与渲染逻辑逐字节不变，`BUILTIN_TOOL_IDS` 仍为原 9 个名称。

**测试**：`test_report_evidence.py` —— 全部 `[n]` 均有对应证据、无证据结论被跳过、非法类型 fail closed、跨结论复用证据同号、`ReportOutput` 校验、失败条目忽略与空结果「证据不足」；纯内存运行，无 DB/网络/休眠。

### P3 退出验收（已实现）

**入口语义**：`POST /api/v1/agents/{id}/test` 现在按 Agent 类型分流——`agent_type=="supervisor"` 时跑一整轮编排（一次 planner + 五个领域子 Agent），其它类型保持原有单 Agent 试跑语义，响应字段不变（`id/status/output/error/trace_id/span_id/total_tokens/total_cost/retry_count`）。

**Supervisor 自落库**：`Supervisor` 新增 keyword-only `agent_id` / `agent_version` / `tenant_id`；传入 `session + agent_id` 时在编排结束（含 `partial` / `failed`）后写且只写一行 `agent_runs`（`output` 转纯 JSON、`retry_count=0`、成本不臆测保持 `None`），并把 `result.agent_run_id` 回填到响应 `id`。落库失败不打断编排：回滚 + 记日志 + `errors` 追加 `supervisor_run_persist_failed: ...`。

**按 Agent 选 fixture**：`Supervisor._default_runner` 读取子 Agent `config["mock_fixture"]`，非空时构造独立 `LLMClient(mode=settings.llm_mode, fixture_dir=settings.llm_fixture_dir, fixture_name=<fixture>)`；未配置时沿用构造注入的 `self._llm`，注入 runner 的既有单测不受影响。

**共享会话串行化**：五个 `Send` 分支复用同一请求级 `AsyncSession`，而 SQLAlchemy 会话并非并发安全（并发 flush 会报 `Session is already flushing`）。`Supervisor` 持有一把 `asyncio.Lock`：`_load_published_agent` 与该锁互斥，`run_agent` 新增可选 `db_lock` 参数只包住「写运行行 + commit」，LLM 调用阶段不持锁——分支仍并行，DB 操作串行；不传 `db_lock` 时单 Agent 试跑行为不变。

**验收断言**：`tests/e2e/test_agent_flow.py`（`@pytest.mark.e2e`）用六个确定性 fixture 播种六个已发布 Agent，发一次请求后断言——`agent_runs` 中该 `trace_id` 恰有 6 行且每 Agent 一行、全部 `succeeded`；`GET /observability/traces/{trace_id}/tree` 展平后恰有 6 个 `span_type=="agent"`（`supervisor.run` + 五个 `agent.<type>`），五个子 span 的父 span 均为 `supervisor.run`、根为 `workflow`；`build_report` 的每个 `[n]` 都落在证据范围内且证据类型合法；输出与 fixture 逐字段一致（不编造数据）。

**运行方式**：`make e2e` = `cd backend && pytest tests -q -m e2e`；新用例已存在，不再返回 `no tests ran`（退出码 5）。

---

## P4 边缘与设备（契约级）

| Task | 交付物 | 文件 | 关键约束 | 失败测试 |
|---|---|---|---|---|
| 17 | edge-agent 包（注册/心跳/outbox） | `edge-agent/edge_agent/*` | 5s 心跳、LWT、断网写 SQLite `pending_messages` 按 `seq` 补发 | `test_outbox.py`：断网 5 分钟后恢复，事件不重不丢 |
| 18 | MQTT 客户端与 handlers + 节点/设备 API | `app/messaging/mqtt/*`、`app/api/v1/{nodes,devices}.py` | `(node_key, msg_id)` 去重；心跳 15s 未到置 offline；下行指令带 `task_id` 并透传 `traceparent` | `test_heartbeat.py`、`test_nodes.py`（重复注册幂等） |

**P4 退出验收**：`make demo` 后 `/nodes` 显示 2 个在线节点；杀掉 edge-agent 容器 15s 后状态转 `offline`；重启后恢复 `online` 且事件无重复。

### Task 17 代码级展开（已实现）

- 包结构：`edge-agent/edge_agent/{config,topics,outbox,mqtt_client,registry,heartbeat,simulator,main}.py`；`devices/`、`pipeline/` 仅为 Task 18/19 占位，无假实现。
- Outbox 表：`pending_messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, topic, payload, qos, retain, created_at, attempts DEFAULT 0, last_error)`；WAL + `busy_timeout=5000`，`threading.Lock` + `check_same_thread=False`。
- 入队补全：dict 载荷缺 `msg_id` 时补 uuid4 hex、缺 `sent_at` 时补 UTC ISO，入库即为补全后的载荷，重放内容与断网时一致；重复 `msg_id` 允许存在，由服务端去重。
- ack 语义：`flush` 按 `seq` 升序逐条 `publish`；成功即 `ack`（DELETE），异常 `fail`（`attempts+1`、写 `last_error`）并停止本轮，剩余留待下次重连；已 ack 的 `seq` 永不重发，整体为至少一次投递。
- 心跳：`HeartbeatPublisher` 默认 `interval_s=5.0`，`tick()` 以 QoS 0 / retain=False 发布；离线直接丢弃、不落 outbox。`collect_metrics()` 在 psutil 不可用或单项读取失败时为 `None`，绝不伪造 0；`gpu` 无真实探针恒为 `None`。
- 注册：`edge/{node_key}/register` QoS 1；每次构建新 `msg_id`，`node_key/os/arch/agent_version` 稳定，离线写入 outbox 等重连补发。
- LWT / 重连：构造时在 `edge/{node_key}/status` 设 retained `offline`；`on_connect` 发布 retained `online`、订阅 `edge/{node_key}/cmd/+`（QoS 1）并同步 `flush` outbox。`reconnect_delay(attempt)` 为 1,2,4,8,16,30,30，paho `reconnect_delay_set(1, 30)`。
- 按主题持久化：register / event / task ack 失败入队；heartbeat / status 不持久化（过期数据无补发价值）。
- 测试与入口：`edge-agent/tests/test_outbox.py` 覆盖 5 分钟断网不丢不重、失败重试不重复、离线注册/事件补发、5s 心跳字段、LWT 与退避；`make test-edge` 独立运行，不依赖网络或数据库。模拟事件额外携带 `occurred_at`（与 `sent_at` 同源）并保留 `simulated=true`，满足控制面 `vision_events` 的入库契约，同时不伪造检测置信度。

### Task 18 代码级展开（已实现）

- 分层与文件：`app/messaging/mqtt/{topics,dedupe,handlers,client,sweeper}.py` 承担接入层；节点/设备走 `api/v1/{nodes,devices}.py` → `services/{node,device}_service.py` → `repositories/{node,device}_repo.py` → 既有模型。**未新增表或迁移**，`edge_nodes` 沿用 `(tenant_id,node_key)`、`vision_events` 沿用 `(tenant_id,dedup_key)` 唯一约束。
- 主题与订阅：上行 `edge/{key}/register`、`heartbeat`、`status`、`event`、`task/ack`；控制面订阅 `edge/+/register`(QoS1)、`$share/cp/edge/+/heartbeat`(QoS0)、`edge/+/status`(QoS1)、`edge/+/event`(QoS1)、`edge/+/task/ack`(QoS1)。下行固定 `edge/{key}/cmd/{action}`。
- 去重：Redis `SET NX EX`，TTL 取 `MQTT_DEDUPE_TTL_SECONDS`（默认 86400），键含租户 `mqtt:dedupe:{tenant}:{node}:{msg}`；Redis 异常时切换进程内有界 LRU 并熔断 30s，handler 可注入 `MemoryDedupe` 测试。视觉事件另由 `dedup_key` 唯一约束兜底。
- handler 语义：register 按 `(tenant_id,node_key)` 幂等 upsert，重复 `msg_id` 返回 duplicate、新消息更新版本/名称/IP；heartbeat 原样保存指标（剔除 `msg_id/sent_at/node_key` 信封字段）并刷新 `last_heartbeat_at`；status 接受 LWT 裸字符串 `offline/online`；event 校验设备属于同租户同节点后入库；task ack 仅去重与日志，工作流接线留给 P6。禁用节点不会被注册/心跳/上线消息复活。
- 客户端与背压：paho `CallbackAPIVersion.VERSION2`，回调线程只把消息塞进容量 1000 的 `asyncio.Queue`，异步 worker 逐条处理，每条独立 session 并在异常时 rollback；`connect_async` + `reconnect_delay_set(1, 30)`，启动/断连均不阻断 API；`publish_command` 自动补 `msg_id/sent_at`，未连接返回 False 由 API 转 503。
- 离线扫描：`NodeSweeper` 每 5s 调用可独立测试的 `sweep_once`，仅把 `last_heartbeat_at < now-15s` 的 online 节点置 offline，disabled 与无心跳节点不动。
- 租户边界：MQTT 线程无请求上下文，统一显式使用 `settings.tenant_default_id`（MVP）；HTTP 侧租户来自 JWT，跨租户与不存在统一 404。
- API 与审计：节点列表支持 status 过滤与分页，提供详情、metrics、PATCH（name/labels/disabled）与 commands；设备提供列表、创建、更新、删除、probe。写接口限 admin/operator，viewer 403；命令 `task_id` 默认 uuid4，`traceparent` 优先级为请求体 → 请求头 → 活动 trace → 新生成；审计 `node.command` / `device.probe` 走旁路，失败不阻断主流程。
- 配置与生命周期：新增 `MQTT_ENABLED`(默认 false)、`MQTT_DEDUPE_TTL_SECONDS`、`NODE_OFFLINE_AFTER_S`(15)、`NODE_SWEEP_INTERVAL_S`(5)；compose 的 api 服务显式 `MQTT_ENABLED=true`；lifespan 仅在开启时启动 MQTT/sweeper，启动失败只记日志，停机顺序为先 sweeper/MQTT 再 tracer。
- 测试：`tests/integration/test_heartbeat.py` 覆盖注册幂等/版本更新/重复消息/租户隔离、心跳指标、15s 扫描边界、禁用不复活、LWT 上下线、事件去重与任务 ack；`tests/integration/test_nodes.py` 覆盖重复注册仅 1 条、跨租户 404、指令发布与审计、viewer 403、禁用 409、MQTT 503、metrics、PATCH 启停用、设备 CRUD/probe。下行测试通过依赖覆盖注入 fake publisher，不依赖 broker。

### P4 退出验收（已实测）

- 环境：Docker Mosquitto 1883 + 本地 uvicorn（`MQTT_ENABLED=true`，8010）+ `edge_agent.main --simulate --nodes 2`；控制面 MQTT 日志确认 `MQTT 已连接`、两节点 `register=created`、heartbeat `updated`、模拟事件 `created`。
- 两节点在线：`edge_nodes` 中 `sim-node-1/2` 均为 `online`，`last_heartbeat_at` 持续刷新；`vision_events` 每条均带 `simulated=true` 与 `occurred_at`，事件数与 `count(distinct dedup_key)` 相等。
- 15s 离线：`docker pause` Mosquitto 模拟心跳中断（LWT 无法送达），23s 后两节点均被 sweeper 置 `offline`；`docker unpause` 后自动重连恢复 `online`。
- 重启幂等：停止边缘进程后 LWT 将两节点置 `offline`；重启后恢复 `online`，`vision_events` 由 28 增至 30 且 `dedup_key` 无重复（dupes=0），outbox 每次仅补发 1 条注册消息。
- 说明：本阶段用本地进程完成退出验收；2 个 edge-agent 容器 + `make demo` 的 Compose 化随 P7 离线部署包落地，避免在未实现 seed/认证演示数据前提前固化 demo profile。

---

## P5 视觉与 RAG（契约级）

| Task | 交付物 | 文件 | 关键约束 | 失败测试 |
|---|---|---|---|---|
| 19 | MockDetector + 4 类规则 + 截图 | `app/vision/*` | 射线法判区域；越线用前后帧叉积符号；去重键 `device:rule:track_id:5min桶`；ffmpeg 失败降级为占位图 | `test_vision_rules.py`：多边形边界点、越线方向、同目标去重 |
| 20 | 知识库入库 + 混合检索 + seed | `app/rag/*`、`app/api/v1/knowledge.py`、`scripts/seed_demo.py` | 向量 Top20 + 全文 + pg_trgm → RRF → rerank Top8；**空结果返回 `[]` 不编造** | `test_rag.py`：引用带 chunk_id；空库时返回空并显式说明 |

**P5 退出验收**：`make seed` 后 `/events` 有 200 条（区域分布不均）；`POST /knowledge/search` 返回带 `chunk_id` 与分数；`retrieval_records` 有对应行（观测计划 `0004` 之后）。

**已验收（真实库）**：`vision_events=200`；`消防通道` 检索返回 `chunk_id=10`、`score=0.016393`；用真实 `Tracer` 发起一次 traced 检索后 `retrieval_records` 新增 1 行（`span_id` 16 位、`retrieved_documents` 2 条、`reranked_documents` 首条分数非空），对应 `retrieval.hybrid` span 已关联 trace。说明：按观测规范，独立 `POST /knowledge/search` 不是 trace 生产点，未处于 trace 中时只做检索、不写 `retrieval_records`；记录由 Agent/工作流内的检索调用产生。

### Task 19 代码级展开（已实现）

- 新增 `app/vision/{geometry,detector,rules,snapshot,service}.py`，全部不依赖 numpy/ultralytics；bbox 统一 `(x,y,w,h)`。
- 几何：`point_in_polygon` 射线法 + 边/顶点容差判定为“在内”；`segments_intersect` 同时校验叉积符号与线段真实相交，避免延长线误报。
- 规则：`ZoneRule`（中心点在多边形内）、`LineRule`（前后帧叉积符号变化 + 相交，payload 带 `positive_to_negative|negative_to_positive`）、`DwellRule`（跨帧累计、离开清零、桶内一次）、`CountRule`（连续 N 帧、桶内一次，无 track_id 也可工作）。缺 track_id 时 zone/line/dwell 不产事件，不编造 ID。
- 去重：`bucket_for` 取 UTC `int(timestamp//300)`；`dedup_key = device:rule:track_id:bucket`，count 为 `device:rule:bucket`；`RuleEngine` 按设备/桶二次去重，跨桶可再发。
- 检测器：`MockDetector` 按脚本确定性回放、无脚本显式空列表，`model_version="mock-v1"` 且 `is_mock=True`；`YoloDetector` 构造/检测直接 `NotImplementedError`；`create_detector` 未知类型抛 ValueError。
- 截图：`capture_snapshot` 只把用户 URI 当 ffmpeg 输入，输出路径由 `storage_dir/snapshots/events/YYYY-MM-DD/{device}-{uuid}.jpg` 生成；ffmpeg 缺失/超时/非零退出时用 Pillow 生成纯色占位图，`degraded=True` 并保留真实 error，不画检测框。`resolve_snapshot_path` 拒绝绝对路径与 `..` 穿越。
- 入库：`ingest_frame` 不重跑检测，只把 Frame 交给规则引擎；`dedup_key` 冲突用 savepoint 幂等跳过；mock 事件 payload 强制 `simulated/mock=true`，截图尝试时写 `snapshot_degraded`（失败附 `snapshot_error`）。不新增表、不加迁移。
- 事件 API：`GET /events`（分页 + start/end/node/device/type/severity/status 筛选，occurred_at desc,id desc）、`GET /events/{id}`、`POST /events/{id}/ack`（admin/operator，new→acked 幂等，旁路审计 `event.ack`）、`GET /events/{id}/snapshot`（FileResponse image/jpeg|png，缺失/穿越 404）。viewer 可读、写接口 403。
- 测试：`tests/unit/vision/test_vision_rules.py`（17 条，几何/越线/去重/dwell/count/detector/截图降级与穿越）、`tests/integration/test_events_api.py`（9 条，筛选分页/租户隔离/ack 权限与幂等/快照流与 404/ingest 幂等与降级标记）。全量 202 passed，`alembic check` 无漂移。

### Task 20 代码级展开（已实现）

- 分层与文件：`app/rag/{errors,parsing,chunking,embedding,reranking,retriever,service}.py`；API/服务/仓储分别落在 `api/v1/knowledge.py`、`services/knowledge_service.py`、`repositories/knowledge_repo.py`，未把 SQL 写进路由。
- 解析：`.md/.txt` 按 UTF-8 解码，PDF 用 PyMuPDF 逐页抽取文本；空文档、乱码/损坏 PDF、`.docx`（未安装 python-docx）均抛显式 `AppError`，不返回空壳成功。上传入口分块读取，超过 10MB 或 0 字节直接 400。
- 分块：按 Markdown 标题切段，相邻小段先合并成块，再在块内做 400–600 字、目标 500 字、重叠 80 字的滑窗；`start/end` 保留原文偏移，`chunk_no` 从 0 递增，标题路径写入 `meta.heading`。演示文档实测块长 469/450/411 字，均在目标区间。
- 入库幂等：`doc_id = sha256("{tenant_id}:{filename}:{text}")[:32]`；重复上传返回已有块数与真实 embedding 状态，不重复写。写完统一 commit，异常 rollback，不留半截数据；审计只记录 doc_id/文件名/块数，不含正文。
- 向量/重排：embedding provider 接口固定 1024 维，默认 `NullEmbeddingProvider`；reranker 默认 `NullRerankerProvider`。没有模型时 `embedding=NULL`、跳过向量腿，返回 `degraded=["vector_unavailable","reranker_unavailable"]` 与中文说明；**不生成随机/哈希/假向量，不伪造 cosine/rerank 分数**。
- 检索：pgvector `1 - cosine_distance`（仅真实 embedding 存在时）、`ts_rank(to_tsvector('simple', content), plainto_tsquery(...))`、`word_similarity(query, content)` 三腿各取 Top20，RRF（k=60）融合后取 Top8；返回 `chunk_id/doc_id/title/source/chunk_no/snippet/score/scores{vector,fts,trigram,rrf,rerank}`，`score` 默认即真实 RRF 值。空库返回 `results=[]` 且 `empty_reason="knowledge_base_empty"`，有库无命中返回 `empty_reason="no_match"`，两种情况都不生成文档。
- 租户与权限：所有 `knowledge_chunks` 查询/删除都带 `tenant_id`；跨租户删除返回 404。`POST /knowledge/documents`、`DELETE /knowledge/documents/{doc_id}` 仅 admin/operator，viewer 403；`GET /knowledge/documents` 与 `POST /knowledge/search` 登录用户可读。
- 工具复用：`search_knowledge_base` 改为调用 `RagService.search`，输出仍含 `chunk_id/source/snippet/score`，并新增 `scores/empty_reason/degraded/strategy/note`；无命中返回空列表与说明，不编造引用。
- 观测接线：`HybridRetriever` 开 `retrieval.hybrid`（`span_type="retrieval"`）并 `record_retrieval`；`0004_obs_retrieval.py` 建 `retrieval_records` 与 `pg_trgm` GIN 索引，未新增业务表、未建跨表外键。详细字段见观测计划 Task 10 展开。检索对观测故障完全隔离：tracer 初始化/`start_span` 失败回退 `NullSpan`，`set_input/set_output/set_error/record_retrieval/finish` 全部经 `_obs_safe` 吞掉异常并记 WARNING，**埋点异常不会改变检索结果或把业务异常替换掉**。
- Seed：`scripts/seed_demo.py` 使用稳定 `dedup_key/node_key/doc_id`，重复运行幂等；200 条事件严格近 24h，区域分布 90/60/35/15，2 个节点、4 台相机，仅 20 条事件挂 Pillow 生成的 `DEMO SNAPSHOT` 占位图，事件 payload 含 `{"simulated": true, "seed": "demo"}`。3 篇中文演示文档走真实 RAG 入库。截图跨天重跑不按新日期建目录，只按库内原路径补缺失文件。
- 测试：`tests/unit/rag/test_rag_units.py` 7 条（解析/PDF/拒绝格式/分块范围与重叠/标题/RRF 纯函数）；`tests/integration/test_rag.py` 11 条（上传列表检索、租户隔离、重复幂等、显式错误、权限与跨租户 404、空库/无命中、top_k 校验、手写真实向量腿、真实 reranker 分数、工具复用、英文 FTS、seed 幂等可检索与跨天截图不增长）；`tests/integration/observability/test_retrieval_recorded.py` 4 条。新增 22 条全部通过，全量 **224 passed**，`alembic upgrade head` 至 `0004_obs_retrieval` 后 `alembic check` 输出 `No new upgrade operations detected.`。
- 实测 seed 两次：第一次 `events_total=200, snapshot_paths=20, snapshots_written=20, docs=3, docs_created=3, chunks=3`；第二次 `events_created=0, snapshots_written=0, docs_created=0, chunks=3`，无翻倍。真实库查询确认 200 条事件、20 个非空 `snapshot_path`、20 张演示 PNG、3 个知识块且 `embedding IS NULL`。
- 真实库 TestClient 检索（覆盖 `get_current_user` 为 viewer，演示库未建用户）：`消防通道` → `chunk_id=10, score=0.01639344262295082`（`trigram=0.8`，`vector/fts/rerank=null`）；`人群聚集` → `chunk_id=11, score=0.01639344262295082`。均返回 `strategy="hybrid_rrf"` 与降级说明，无伪造分数。

---


## P6 工作流引擎与前端（契约级）

| Task | 交付物 | 文件 | 关键约束 | 失败测试 |
|---|---|---|---|---|
| 21 | DSL + 校验器 | `app/workflow/{dsl,validator,expression}.py` | 6 种节点；单 start、至少一个 end；无环、可达；表达式白名单（无函数调用、无属性链写） | `test_dsl_validator.py`：环、不可达、非法表达式均被拒 |
| 22 | 引擎 + 审批门 + Celery 任务 | `app/workflow/{engine,executor}.py`、`app/tasks/workflow_tasks.py` | 建 run 时预插全部 node_run；审批命中 → `waiting_approval` 释放 worker；`(run_id,node_id,attempt)` 幂等；重启扫 `heartbeat_at` 超 60s 重投 | `test_workflow_engine.py`、`test_workflow_timeout.py` |
| 23 | 前端壳与登录 | `web/src/{main.ts,router,layouts,views/Login.vue,api/http.ts,stores/auth.ts}` | 401 自动刷新；路由守卫按角色；三态组件 | `npm run build` 通过 + `test_http_interceptor.spec.ts` |
| 24 | 工作流编辑器 | `web/src/views/WorkflowEditor.vue`、`components/workflow/*` | Vue Flow；按 `input_schema` 渲染参数表单；校验/发布/试跑 | `test_workflow_editor.spec.ts`：拖 6 节点并校验通过 |
| 25 | 运行追踪页 | `web/src/views/{Runs.vue,RunDetail.vue}`、`components/runs/*` | 节点树实时状态（WS）、耗时、token、工具调用、审批按钮 | `test_run_detail.spec.ts`：审批后状态从 `waiting_approval` → `running` |

**P6 退出验收**：在界面上拖出 `start → vision(agent) → condition → approval → report(agent) → end`，发布、触发、审批、拿到 Markdown 报告；`workflow_runs.trace_id` 与报告里的证据 ID 均可点击查看。

---

## P7 分析、部署与可靠性（契约级）

| Task | 交付物 | 文件 | 关键约束 | 失败测试 |
|---|---|---|---|---|
| 26 | Dashboard/审计/用户 API | `app/api/v1/{dashboard,users}.py` | 统计全部走 SQL 聚合，禁止全量拉取 | `test_dashboard_summary.py`：数字与直接 SQL 抽查一致 |
| 27 | 观测 profile + 离线包 + 保留脚本 | `docker/compose/docker-compose.observability.yml`、`scripts/{retention.py,offline_pack.sh}` | `retention --dry-run` 打印将删除行数；离线 `docker save/load` 后 `compose up` 可跑核心流程 | `test_retention_dry_run.py` |
| 28 | 文档与演示 | `docs/{architecture,er,api,runbook,offline-install,eval,demo-script}.md`、`README.md` | `docs/eval.md` 必须有 3 个实测指标（事件 p95、6 Agent p95、断网完整率） | 人工检查：按 README 5 分钟跑通 |

**P7 退出验收**：`make verify` 全绿；`docs/eval.md` 三项指标有实测值（Mock LLM 与真实 LLM 分开标注）；断网 5 分钟事件完整率 100%、重复率 0。

---

## Self-Review（已执行）

- **Spec 覆盖**：v2 §1（部署/异步/租户/HITL/编排/通知/分层/TDD）→ Task 1–28；§2 16 表 → Task 5；§5 观测字段变更 → Task 5 清单；§6 API → Task 4/8–12/26；§7 引擎 → Task 21–22；§8 Agent/工具 → Task 9–16；§9 边缘 → Task 17–18；§10 视觉 → Task 19；§11 RAG → Task 20；§12 前端 → Task 23–25；§13 测试 18 项 → 平台各 Task 测试 + P3–P7 退出验收，逐条落点见观测计划「需求 18 项测试的落点」（本计划负责 2、3、4、5、6、11、17 的埋点侧）；§14 阶段 → 本文件 Phase 划分。
- **占位符扫描**：无 TBD/TODO；P3–P7 的"进入前展开"是显式 Gate 规则，不是遗漏。
- **接口一致性**：`tracer.start_span(name, span_type)`、`SpanHandle.record_llm_call/record_tool_call/record_retrieval`、`ToolResult(ok, data, error_code, attempts)`、`AgentRunResult(agent_run_id, output, span_id, trace_id)` 在平台与观测两份计划中同名同义。
