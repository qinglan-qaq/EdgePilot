# EdgePilot 设计文档（MVP）

- 状态：待评审
- 日期：2026-11-25
- 仓库：`E:\EdgePilot`
- 目标岗位对标：辛米尔科技（Simmir）全栈工程师 / 端侧 AI 平台
- **注意**：§3 架构、§5 数据模型、§7 HITL、§8 Agent 编排、§9 边缘协议已被
  `2026-11-25-edgepilot-architecture.md`（v2）取代，架构评审以 v2 为准。

---

## 1. 项目是什么

EdgePilot 是一个面向工业视觉、IoT 与边缘计算场景的 **AI Agent 管理 + 工作流编排 + 边缘节点管理** 平台。

一句话：**把「摄像头 / 设备 → 视觉事件 → 多 Agent 协作研判 → 报告与工单」做成一条可配置、可下发、可观测、可离线部署的链路。**

它不是一个聊天机器人，也不是一个 Agent 框架的 demo。判断它是否成立的唯一标准是：
**一条真实的业务请求从界面进入，经过 6 个 Agent 协作、调用真实数据与知识库，最终产出一份每句话都能追溯到事件 ID 的报告。**

### 1.1 明确不做（YAGNI）

| 不做 | 原因 |
|---|---|
| YOLO 模型部署与训练 | 用户已明确本期不部署；只保留检测器抽象与真实截图链路 |
| 多租户 | 单租户运行，不建 `tenant_id` 列；二期通过 Alembic 加列 + 数据回填迁移 |
| Kubernetes 生产部署 | 只交付 Helm Chart 骨架 + 单节点 k3s 文档，不作为验收项 |
| 视频回放 / 云台控制 / 录像检索 | 本平台不是 VMS |
| Celery / RabbitMQ | 与工作流执行引擎职责重叠，Redis Stream 足够 |
| 微服务拆分 | 首期是模块化单体 + 独立 worker 进程 |
| GPU 推理 | 本机 3.11 栈只有 CPU torch；视觉不部署 YOLO，无需 GPU |

---

## 2. 已确认的决策（19 条）

| # | 决策 | 取值 |
|---|---|---|
| 1 | Python 版本 | 3.11（本机 3.11.15） |
| 2 | 视觉推理 | 不部署 YOLO，只做检测器抽象 + 真实截图 + 规则 |
| 3 | 运行环境 | 克隆 `lawApp_LangGraph` → 新 conda env `edgepilot`，CPU torch |
| 4 | 需求基准 | 以用户 2026-11-25 提供的《EdgePilot》提示词为准 |
| 5 | 监控 | `/metrics` + 自建指标页必做；Grafana 进 `observability` profile |
| 6 | Embedding | 本地 `bge-large-zh-v1.5`（1024 维）+ `bge-reranker-large` |
| 7 | Agent 数量 | 6 个全保留：Supervisor / Device / Vision / Diagnosis / Operations / Report |
| 8 | 数据表 | 16 张（见 §5） |
| 9 | MCP | 真实接入：自建最小 MCP Server，stdio，2 个工具 |
| 10 | HITL | **引擎级审批门**（`workflow_node_runs` 状态机 + Redis Stream 唤醒） |
| 11 | 视觉事件来源 | Detector 协议 + MockDetector + ffprobe/ffmpeg 真实抽帧截图 |
| 12 | LLM | DeepSeek API 优先，无 key / 断网自动降级到录制回放 Mock |
| 13 | 前端 UI 库 | Element Plus |
| 14 | 异步机制 | Redis Stream + 单 worker + DB 状态机 |
| 15 | 权限模型 | 代码内置 3 角色 `admin/operator/viewer`，`users.role` 字段 |
| 16 | 报告导出 | Markdown 必做；PDF 走浏览器打印样式 |
| 17 | 数据保留 | 事件 90 天、运行记录 180 天、审计 365 天 |
| 18 | 离线交付 | Phase 7 交付 `save/load` 脚本 + 断网演练记录 |
| 19 | 测试门槛 | 核心模块覆盖率 ≥70%，整体 ≥50%，12 项必测，1 条 E2E |

---

## 3. 系统架构

```
┌────────────────────────────── Web（Vue3 + TS + Element Plus）──────────────────────────────┐
│ Dashboard │ Agent │ 工作流编辑器(Vue Flow) │ 运行追踪 │ 节点/设备 │ 事件 │ 知识库 │ 用户 │
└───────────┬──────────────────────────────────────────────────┬─────────────────────────────┘
            │ REST /api/v1            WebSocket /ws/events        │ SSE /ws/runs/{id}
┌───────────▼──────────────────────────────────────────────────▼─────────────────────────────┐
│                        control-plane（FastAPI，模块化单体）                                 │
│  api/  services/  agent_runtime/  workflow_engine/  tool_registry/  rag/  messaging/       │
│  auth( JWT + RBAC )  ·  audit  ·  metrics(/metrics)                                        │
└───┬──────────────┬───────────────────┬───────────────────┬──────────────────┬──────────────┘
    │              │                   │                   │                  │
┌───▼────┐  ┌──────▼──────┐   ┌────────▼────────┐  ┌───────▼───────┐  ┌───────▼───────┐
│ PG16   │  │  Redis 7    │   │  MQTT Broker    │  │  MinIO/本地盘 │  │  MCP Server   │
│pgvector│  │Stream+Cache │   │  (EMQX/Mosq)    │  │  截图/文档    │  │  (stdio)      │
└────────┘  └─────────────┘   └────────┬────────┘  └───────────────┘  └───────────────┘
                                       │ MQTT 上下行（心跳/指令/事件）
                              ┌────────▼─────────┐
                              │  edge-agent ×2   │  Python，Linux / Windows
                              │ 设备接入/抽帧/上报 │  模拟模式：视频文件 + MockDetector
                              └──────────────────┘
```

### 3.1 进程划分（同一代码库，不同入口）

| 进程 | 入口 | 职责 |
|---|---|---|
| `api` | `python -m app.main` | HTTP / WS、鉴权、CRUD、查询 |
| `worker` | `python -m app.worker` | 消费 Redis Stream：执行工作流、跑 Agent、跑 RAG 入库 |
| `edge-agent` | `edge_agent.main` | 边缘节点：注册、心跳、设备探测、抽帧、事件上报 |
| `mcp-server` | `mcp_server.main` | 暴露 2 个 MCP 工具给 Tool Registry |

> worker 与 api 共享代码，但独立进程。理由：Agent 执行是长任务，不能和 API 事件循环抢资源，也不能让一个卡住的 Agent 拖垮管理面。

### 3.2 请求流（核心场景）

```
用户："分析今天 9-12 点哪个区域人员闯入最多，并找出未处理的高风险事件"
  → POST /workflows/{id}/runs          创建 workflow_run(trace_id)
  → Redis Stream wf:runs               入队
  → worker 取任务                       执行 DAG
      → agent 节点 Supervisor            LangGraph: plan → dispatch → synthesize
          → 工具 query_vision_events     Tool Registry → 权限 → 参数校验 → 执行 → 审计
          → 工具 query_device_status     （其中 2 个工具经 MCP Server 调用）
      → condition 节点                   判断是否有高风险事件
      → approval 节点（若命中）           run → waiting_approval，worker 释放
      → 人工在 UI 审批                    POST /runs/{id}/approve → 重新入队
      → agent 节点 Report                生成 Markdown，写入 node_run.output
  → run → succeeded                     WS 推送进度到前端
  → 前端展示执行链 + 报告 + 每条结论的事件 ID 引用
```

---

## 4. 运行时与部署

### 4.1 本地开发

```bash
conda create -n edgepilot --clone lawApp_LangGraph -y
conda activate edgepilot
pip install paho-mqtt alembic redis aiosqlite "python-jose[cryptography]" "passlib[bcrypt]"
# 可选（本期不启用 YOLO / OpenCV 路径，仅在切到真实解码器时安装）
# pip install onnxruntime opencv-python

# 只用 infra 起依赖
cd deploy && docker compose --profile core up -d

# 应用
alembic upgrade head
python -m app.main            # API :8000
python -m app.worker          # worker
python -m edge_agent.main --simulate   # 模拟节点 ×2
python -m mcp_server.main     # MCP server（worker 以 stdio 拉起，一般不用手动起）
```

> 若 `--clone` 后 sentence-transformers 报 torch 版本问题，回退方案：新建 `edgepilot` 环境，安装 `torch==2.6.0+cpu`（本机 `langchain_env` 已有该 wheel 可参考），其余包按 requirements 装。

### 4.2 Docker Compose profiles

| profile | 服务 |
|---|---|
| `core` | postgres(pgvector/pgvector:pg16) · redis · mosquitto · api · worker · web |
| `observability` | prometheus · grafana |
| `demo` | 2 个 edge-agent 容器 + 事件种子脚本 |

### 4.3 环境变量

`.env.example` 必须覆盖：`DATABASE_URL` `REDIS_URL` `MQTT_*` `JWT_SECRET` `DEEPSEEK_API_KEY` `LLM_MODE(real|mock|auto)` `HF_HOME` `EMBEDDING_MODEL` `RERANKER_MODEL` `STORAGE_DIR` `SNAPSHOT_TTL_DAYS`。

**密钥一律从环境变量读取，`model_configs.api_key_enc` 只存加密后的值，日志中脱敏。**

---

## 5. 数据模型（16 张表）

约定：所有表含 `id bigserial PK`、`created_at timestamptz`、`updated_at timestamptz`（`audit_logs`/`knowledge_chunks` 只保留 `created_at`）。时间统一 UTC。

### 5.1 表清单

| # | 表 | 关键字段 | 索引 / 约束 |
|---|---|---|---|
| 1 | `users` | email, password_hash(argon2/bcrypt), display_name, role(admin/operator/viewer), status | `email` unique |
| 2 | `agents` | name, agent_type, model_config_id FK, prompt_system, config jsonb, status, current_version | `name` unique；`agent_type` 普通索引（内置 6 条种子数据，允许同类型多实例，如按设备范围拆两个 Vision Agent） |
| 3 | `agent_versions` | agent_id FK, version, snapshot jsonb（prompt+model+tool_ids+config）, created_by | `(agent_id, version)` unique；**发布后不可 UPDATE**（应用层 + 触发器） |
| 4 | `model_configs` | name, model_type(llm/embedding/reranker), provider, model_name, base_url, api_key_enc, params jsonb, deployment_location, status | `name` unique |
| 5 | `tools` | tool_id, name, description, source(local/mcp), input_schema jsonb, output_schema jsonb, permission_scope, timeout_ms, max_retries, version, enabled, handler | `tool_id` unique |
| 6 | `agent_tool_bindings` | agent_id FK, tool_id FK, config jsonb, enabled | `(agent_id, tool_id)` unique |
| 7 | `workflows` | name, description, status(draft/published/archived), current_version, created_by | `name` unique |
| 8 | `workflow_versions` | workflow_id FK, version, dsl jsonb, dsl_hash, created_by | `(workflow_id, version)` unique；发布后不可变 |
| 9 | `workflow_runs` | workflow_version_id FK, trigger_type, trigger_payload jsonb, status, input jsonb, output jsonb, error, trace_id, started_at, finished_at | `(status, created_at desc)`；`trace_id` index |
| 10 | `workflow_node_runs` | run_id FK, node_id, node_type, status, attempt, input jsonb, output jsonb, approval jsonb, error, started_at, finished_at | `(run_id, node_id)` unique；`(status)` partial index（找待审批） |
| 11 | `edge_nodes` | node_key, name, os, arch, agent_version, status, ip, labels jsonb, metrics jsonb, last_heartbeat_at | `node_key` unique；`(status)` |
| 12 | `devices` | node_id FK, name, kind(camera/sensor/other), config jsonb, status, last_seen_at | `(node_id, name)` unique |
| 13 | `vision_events` | node_id, device_id, event_type, severity, occurred_at, received_at, confidence, track_id, snapshot_path, bbox jsonb, model_version, payload jsonb, dedup_key, status | `(occurred_at desc)`；`(device_id, occurred_at desc)`；`dedup_key` unique |
| 14 | `agent_runs` | workflow_run_id, node_run_id, agent_id, agent_version, status, input jsonb, output jsonb, token_usage jsonb, tool_calls jsonb, retry_count, error, trace_id, started_at, finished_at | `(agent_id, created_at desc)`；`tool_calls` GIN |
| 15 | `knowledge_chunks` | doc_id, title, source, chunk_no, content, embedding vector(1024), metadata jsonb | HNSW on `embedding`；`(doc_id, chunk_no)` unique；GIN on `to_tsvector('simple', content)` |
| 16 | `audit_logs` | user_id, action, resource_type, resource_id, ip, user_agent, detail jsonb, trace_id | `(created_at desc)`；`(resource_type, resource_id)` |

### 5.2 事务边界（必须显式）

1. 发布工作流 = 插入 `workflow_versions` + 更新 `workflows.current_version`，同一事务。
2. 发布 Agent = 插入 `agent_versions` + 更新 `agents.current_version`，同一事务。
3. 事件入库 = 插入 `vision_events` + 写审计（去重靠 `dedup_key` 唯一约束，冲突即忽略）。
4. 创建 run = 插入 `workflow_runs` + 预插所有 `workflow_node_runs(status=pending)`，同一事务（这样前端一进来就能看到完整节点树）。
5. 审批决策 = 更新 `workflow_node_runs.approval/status` + 更新 `workflow_runs.status` + 入队，同一事务后再入队（先落库，后发消息）。

### 5.3 数据保留

`scripts/retention.py`：删除 90 天前事件与截图、180 天前 run/node_run/agent_run、365 天前审计。脚本必须支持 `--dry-run`。

---

## 6. API 设计

### 6.1 统一约定

- 前缀 `/api/v1`，响应 `{"code":0,"message":"ok","data":...,"trace_id":"..."}`
- 分页 `?page=1&size=20` → `{"items":[],"total":0,"page":1,"size":20}`
- 列表筛选必须走 query 参数，禁止前端全量拉取
- 错误码：400 参数 / 401 / 403 越权 / 404 / 409 冲突 / 422 校验 / 429 限流 / 500
- 写接口支持 `Idempotency-Key`
- 每个响应头带 `X-Request-Id`，与 `trace_id` 一致

### 6.2 端点清单

**认证**
`POST /auth/login` · `POST /auth/refresh` · `POST /auth/logout` · `GET /auth/me`

**Agent**
`GET/POST /agents` · `GET/PATCH/DELETE /agents/{id}` · `POST /agents/{id}/publish` · `GET /agents/{id}/versions` · `GET /agents/{id}/runs` · `POST /agents/{id}/test`（单 Agent 试跑，返回 agent_run）

**模型**
`GET/POST /models` · `GET/PATCH/DELETE /models/{id}` · `POST /models/{id}/test`（连通性 + 一次最小调用）

**工具**
`GET/POST /tools` · `GET/PATCH /tools/{id}` · `POST /tools/{id}/test` · `GET /tools/{id}/calls`（从 agent_runs.tool_calls 聚合）

**工作流**
`GET/POST /workflows` · `GET/PATCH /workflows/{id}` · `POST /workflows/{id}/validate` · `POST /workflows/{id}/publish` · `GET /workflows/{id}/versions` · `POST /workflows/{id}/runs`

**运行**
`GET /runs` · `GET /runs/{id}`（含节点树） · `POST /runs/{id}/cancel` · `POST /runs/{id}/approve` · `POST /runs/{id}/reject` · `GET /runs/{id}/logs`

**节点 / 设备**
`GET /nodes` · `POST /nodes/{key}/commands` · `PATCH /nodes/{key}`（禁用） · `GET /nodes/{key}/metrics`
`GET/POST /devices` · `PATCH/DELETE /devices/{id}` · `POST /devices/{id}/probe`（ffprobe + 抓一帧）

**事件**
`GET /events`（按时间/节点/设备/类型/严重度/状态筛选） · `GET /events/{id}` · `POST /events/{id}/ack` · `GET /events/{id}/snapshot`（图片流）

**知识库**
`POST /knowledge/documents`（上传） · `GET /knowledge/documents` · `DELETE /knowledge/documents/{doc_id}` · `POST /knowledge/search`（调试用混合检索）

**系统**
`GET /dashboard/summary` · `GET /audit-logs` · `GET /users`（admin） · `POST /users` · `PATCH /users/{id}` · `GET /metrics`（Prometheus）

**实时**
`WS /ws/events?token=`（事件流，按角色过滤） · `WS /ws/runs/{run_id}`（执行进度）

### 6.3 权限矩阵

| 资源 | admin | operator | viewer |
|---|---|---|---|
| Agent / 工具 / 模型 配置 | 读写 | 读 | 读 |
| 工作流 发布 / 触发 | 读写 | 读写 | 读 |
| 审批 / 取消运行 | 写 | 写 | — |
| 节点指令 / 设备配置 | 写 | 写 | 读 |
| 用户管理 / 审计 | 读写 | — | — |
| 知识库 上传 / 删除 | 写 | 写 | 读 |

实现：`require(role, permission)` FastAPI 依赖 + 资源级校验（如 run 归属）。越权一律 403 并写 `audit_logs`。

---

## 7. 工作流引擎（含 HITL）

### 7.1 DSL

```json
{
  "nodes": [
    {"id":"start","type":"start","position":{"x":0,"y":0}},
    {"id":"plan","type":"agent","agent":"supervisor","input_map":{"question":"$input.question"}},
    {"id":"has_risk","type":"condition","expression":"$plan.output.high_risk_count > 0","timeout_ms":5000},
    {"id":"approve","type":"approval","title":"高风险事件需确认","approvers":["operator","admin"],"timeout_ms":86400000},
    {"id":"report","type":"agent","agent":"report","input_map":{"events":"$plan.output.events"}},
    {"id":"end","type":"end"}
  ],
  "edges": [{"from":"start","to":"plan"},{"from":"plan","to":"has_risk"},
            {"from":"has_risk","to":"approve","when":"true"},{"from":"has_risk","to":"end","when":"false"},
            {"from":"approve","to":"report"},{"from":"report","to":"end"}]
}
```

节点类型固定 6 种：`start` / `agent` / `tool` / `condition` / `approval` / `end`。

校验规则（`POST /workflows/{id}/validate` 必须实现）：
- 必须有且仅有一个 `start`、至少一个 `end`
- DAG 无环；所有节点从 `start` 可达；所有节点可到达某个 `end`
- `agent` 节点的 agent 必须存在且 enabled；`tool` 节点的 tool 必须 enabled 且参数匹配 `input_schema`
- `condition.expression` 只允许受限语法（字段访问 + 比较 + 布尔运算），禁止任意代码执行
- 每个节点必须有 `id`、`type`、`timeout_ms`

### 7.2 执行状态机

```
workflow_runs:
  pending ──worker 领取──> running ──> succeeded
                             │  │
                             │  ├──> failed
                             │  ├──> timeout
                             │  ├──> cancelled
                             │  └──> waiting_approval ──审批──> running
workflow_node_runs:
  pending → running → succeeded | failed | skipped
                   └→ waiting_approval → running（恢复）
```

### 7.3 HITL 审批门（引擎级）

1. 执行到 `approval` 节点：`node_run.status = waiting_approval`，`run.status = waiting_approval`，`approval = {"title","approvers","created_at","expires_at"}`，worker 直接释放，不入队。
2. 前端"待我审批"列表来自 `workflow_node_runs WHERE status='waiting_approval'`（partial index）。
3. `POST /runs/{id}/approve|reject` → 校验角色属于 `approvers` → 写 `approval.decision/comment/by/at` → run 状态回 `running` → 重新入队（带 `resume_node_id`）。
4. 恢复时 worker 从 `resume_node_id` 的下游继续，不回放已成功节点。
5. 超时：`approval.timeout_ms` 到期由定时扫描置 `failed(reason=approval_timeout)`，并写审计。
6. 进程重启：worker 启动时把 `status='running'` 且 `heartbeat_at` 超过 60s 的 run 重新入队（幂等：同 `(run_id, node_id, attempt)` 不重复执行）。

**为什么不用 LangGraph interrupt**：审批发生在节点之间，引擎级状态机天然可重启、可查询、可被 API 驱动；LangGraph 图内 interrupt 会把"图线程"变成第二个状态机，恢复需要 thread 亲和，且和 `agent_runs` 的失败重试语义冲突。Agent 内部的高风险工具确认，本期用 `tools.permission_scope='approval_required'` 直接拒绝并返回"需人工确认"，由工作流层审批节点承担。

### 7.4 重试 / 幂等 / 超时

- 节点级 `max_retries`（默认 0，agent/tool 节点默认 2），指数退避 1s/4s/16s
- 每次尝试 `attempt += 1`，独立记录，不覆盖上一轮结果
- `tool` 节点调用必须带 `Idempotency-Key = {run_id}:{node_id}`，工具实现方负责幂等（如 MCP 只读工具天然幂等）
- 节点超时由 worker 内 `asyncio.wait_for` 控制；run 级超时由定时扫描兜底
- 事件消费（MQTT）用 `(node_key, msg_id)` 去重，重复消息直接 ack 丢弃

---

## 8. Agent 与工具

### 8.1 六个 Agent 的职责与工具权限

| Agent | 职责 | 允许工具 | 模型 |
|---|---|---|---|
| Supervisor | 解析任务、拆子任务、派发、汇总、失败重规划 | 无（不直连数据，只做编排） | LLM |
| Device | 设备信息、在线状态、历史状态 | `query_device_status`、`query_device_telemetry`（MCP） | LLM |
| Vision | 视觉事件查询、事件类型识别、关联摄像头与模型版本 | `query_vision_events`、`query_alarm_events` | LLM |
| Diagnosis | 关联设备状态 + 事件 + 历史知识，给可能原因与证据 | `search_knowledge_base`、`query_vision_events` | LLM |
| Operations | 节点资源、服务状态、任务失败记录 | `query_edge_node_metrics`、`query_task_history` | LLM |
| Report | 汇总多 Agent 结果，生成 Markdown 报告 | `generate_report` | LLM |

硬约束（写进代码，不只是 prompt）：
- Supervisor **不能**直接调用数据工具，只能调用 Agent（避免"编排器绕过权限"）
- 任何 Agent 的输出必须通过 Pydantic Schema 校验，校验失败重试 1 次，再失败返回 `agent_output_invalid`
- 诊断结论必须带 `evidence: [{event_id|metric|chunk_id, snippet}]`，无证据的结论字段置空而不是编造
- `tools.permission_scope='approval_required'` 的工具（本期为设备控制类，不实现）直接拒绝

### 8.2 Tool Registry

`ToolRegistry.execute(tool_id, args, ctx)` 固定流程：

```
查 enabled → 查 agent 绑定 → 校验 permission_scope → 校验 input_schema
→ 超时包装 → 重试策略 → 执行（local handler 或 MCP call）
→ 校验 output_schema → 写 agent_runs.tool_calls → 写 audit（高风险工具）→ 返回
```

任何一步失败都返回结构化错误，不抛裸异常。9 个工具，其中 7 个本地实现、2 个经 MCP 暴露：

`query_device_status` · `query_device_telemetry` · `query_edge_node_metrics` · `query_vision_events` · `query_alarm_events` · `query_model_status` · `query_task_history` · `search_knowledge_base` · `generate_report`

### 8.3 MCP 真实接入

- 自建 `mcp_server`（Python，stdio transport），暴露：
  - `query_device_status(device_id? | node_key?)` → 读 `devices` + `edge_nodes`
  - `search_knowledge_base(query, top_k)` → 复用 RAG 服务
- Tool Registry 启动时通过 MCP `list_tools` 自动发现并 upsert 到 `tools` 表（`source='mcp'`）
- 调用走 MCP `call_tool`，与本地工具同一套权限/超时/审计外壳
- **验收证据**：`tools` 表里能看到 2 条 `source='mcp'` 的记录；`agent_runs.tool_calls` 里能看到 `transport:"mcp"`

### 8.4 LLM 接入与降级

- `LLMClient` 统一接口：`chat(messages, response_schema=None, temperature=0)`
- `LLM_MODE=real|mock|auto`；`auto` 时：无 API key 或连续 2 次超时 → 切 mock，并在 UI 顶部提示"降级模式"
- Mock 模式读取 `tests/fixtures/llm/*.json`（key = prompt hash），未命中返回结构化"无法回答"，**绝不返回伪造数据**
- 记录 `token_usage` 到 `agent_runs.token_usage`

---

## 9. 边缘节点与 MQTT

### 9.1 Topic 规范

| Topic | 方向 | QoS | 说明 |
|---|---|---|---|
| `edge/{node_key}/register` | 上行 | 1 | 首次注册（含 os/arch/agent_version） |
| `edge/{node_key}/heartbeat` | 上行 | 0 | 5s，含 cpu/mem/disk/gpu/task_count |
| `edge/{node_key}/status` | 上行 | 1 (retain) | `online` / `offline`（LWT 遗嘱） |
| `edge/{node_key}/event` | 上行 | 1 | 视觉事件或异常 |
| `edge/{node_key}/task/ack` | 上行 | 1 | 任务状态：accepted/running/succeeded/failed |
| `edge/{node_key}/cmd/{action}` | 下行 | 1 | `probe_device` / `start_task` / `stop_task` / `sync_config` |
| `$share/cp/edge/+/heartbeat` | 订阅 | 0 | 控制面共享订阅，支持多 worker |

规则：消息必须带 `msg_id`（uuid）与 `sent_at`；控制面按 `(node_key, msg_id)` 去重；心跳 15s 未到 → `offline`，同时以 LWT 为准（双保险）。

### 9.2 edge-agent 行为

- 启动：读本地配置 → 注册 → 订阅自身 cmd → 5s 心跳 → 上报设备列表
- 设备探测：`POST /devices/{id}/probe` 下发 `cmd/probe_device` → agent 执行 `ffprobe` → 抽一帧 → 回传 `task/ack` + 截图（HTTP 上传，不走 MQTT）
- 任务：收到 `start_task` → 校验 `task_id` 未执行过 → 执行 → 上报状态
- 断线：指数退避重连（1s→30s）；期间事件写本地 SQLite（`pending_messages`），恢复后按 `seq` 补发，服务端幂等
- 模拟模式：`--simulate --nodes 2` 生成两个节点的假心跳与假事件；设备为 `file://` 视频或静态图，Detector 用 Mock
- 真实截图：`ffmpeg -ss {t} -i {uri} -frames:v 1 out.jpg`，失败降级为纯色占位图并标注 `snapshot_degraded=true`

> 不使用 MQTT 传视频本身，符合提示词要求。

---

## 10. 视觉事件

### 10.1 检测器抽象

```python
class Detection(NamedTuple):
    label: str          # person / vehicle / unknown
    bbox: tuple[float, float, float, float]
    confidence: float
    track_id: int | None

class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> list[Detection]: ...
```

- `MockDetector`：按配置的固定轨迹返回检测框（确定性，可复现）
- `YoloDetector`：**只留骨架 + `NotImplementedError` + 接入说明**，不安装 ultralytics
- 选择由 `devices.config.detector` 决定（`mock` / `yolo`）

### 10.2 规则

| 规则 | 判定 | 去重 |
|---|---|---|
| `zone` 区域入侵 | 检测框中心点落在多边形内（射线法） | `device:rule:track_id:5min 桶` |
| `line` 越线 | 前后两帧中心点与线段叉积符号变化 | 同上 |
| `dwell` 停留 | 同 track 在区域内累计时长 ≥ 阈值 | 同上 |
| `count` 数量超限 | 区域内目标数 ≥ 阈值，持续 N 帧 | `device:rule:桶` |

事件必带：`device_id`、`model_version`、`confidence`、`occurred_at`、`snapshot_path`、`bbox`。

### 10.3 演示数据

`scripts/seed_demo.py` 生成：2 个节点、4 台相机、近 24h 的 200 条事件（含分布不均的区域，保证"哪个区域最多"有真实答案）、20 张截图（本地图片缩放生成）。不允许把答案写死在前端。

---

## 11. RAG

- 入库：PDF/MD/TXT → 解析（pymupdf / python-docx）→ 按标题层级 + 递归分块（400–600 字，overlap 80）→ bge-large-zh-v1.5 编码（CPU，batch 16）→ `knowledge_chunks`
- 检索：pgvector 余弦 Top20 + PostgreSQL 全文（`to_tsvector('simple', content)`）+ `pg_trgm` 兜底 → RRF 融合 → bge-reranker-large 精排 Top8
- 返回：`chunk_id`、`source`、`snippet`、`score`；**Agent 引用必须带 chunk_id**
- 空结果：返回 `[]` 并让 Agent 输出"知识库无相关信息"，禁止编造
- 权限：`viewer` 可检索不可删除；上传/删除需 `operator+`

---

## 12. 前端

| 页面 | 路由 | 关键交互 |
|---|---|---|
| 登录 | `/login` | JWT 存储、401 自动刷新 |
| Dashboard | `/` | 真实统计卡片 + 事件趋势 + 最近运行 |
| Agent 管理 | `/agents` `/agents/:id` | 列表/详情、Prompt 编辑、工具绑定、版本发布、试跑 |
| 工作流列表 | `/workflows` | 状态、版本、最近运行 |
| 工作流编辑器 | `/workflows/:id` | Vue Flow 画布、节点面板、参数表单（按 schema 渲染）、校验、发布、试运行 |
| 运行追踪 | `/runs` `/runs/:id` | 节点树实时状态、耗时、token、工具调用、审批按钮、错误 |
| 节点与设备 | `/nodes` `/devices` | 心跳指标、在线状态、指令下发、RTSP 探测抓帧预览 |
| 事件中心 | `/events` | 实时流、缩略图、筛选、详情（截图 + 检测框 + 关联事件） |
| 知识库 | `/knowledge` | 上传、列表、检索调试（看 TopK 与分数） |
| 系统 | `/settings` | 用户、审计日志、降级模式提示 |

工程要求：Vite 多环境、axios 拦截器（401 刷新 / 统一错误）、路由守卫按角色、Pinia 按域拆 store、三态（loading/empty/error）、WS 断线重连 + 重连拉增量、事件流前端节流合并、大列表虚拟滚动。

---

## 13. 测试策略

### 13.1 四层

| 层 | 范围 | 运行时机 | 工具 |
|---|---|---|---|
| 单元 | service / 规则 / DSL 校验 / 工具外壳 / 权限 | 每次提交 | pytest |
| 集成 | 真实 PG+Redis+MQTT（compose），事务、幂等、去重、审批恢复 | 每个 Phase 末 | pytest + testcontainers 或 compose |
| E2E | 登录→建 Agent→跑工作流→审批→报告 | 每个 Phase 末 | pytest + httpx |
| 演练 | 断网、超时、重复消息、节点掉线、LLM 挂掉 | Phase 4 / 7 专项 | 脚本 |

### 13.2 12 项必测（对应测试文件）

| # | 用例 | 文件 |
|---|---|---|
| 1 | 登录 / 刷新 / 越权 403 | `test_auth.py` |
| 2 | 工具参数校验 422 / 超时 / 重试 / 幂等 | `test_tool_registry.py` |
| 3 | 无权限工具被拒且落审计 | `test_tool_permissions.py` |
| 4 | 每次 Agent 运行有 run_id + trace_id，失败有错误链 | `test_agent_runs.py` |
| 5 | 工作流版本不可变；节点失败重试；审批中断恢复 | `test_workflow_engine.py` |
| 6 | 工作流超时置 timeout（不卡 running） | `test_workflow_timeout.py` |
| 7 | 节点重复注册幂等 | `test_nodes.py` |
| 8 | 心跳超时离线；重复心跳/事件不重复计数 | `test_heartbeat.py` |
| 9 | 断网 5 分钟后恢复：事件完整、无重复 | `test_edge_offline.py` |
| 10 | 规则边界：越线方向、多边形边界点、同目标去重 | `test_vision_rules.py` |
| 11 | RAG 引用可追溯；空结果不编造 | `test_rag.py` |
| 12 | 密钥不入日志、SQL 注入、上传路径穿越 | `test_security.py` |

### 13.3 覆盖率与 CI

- 核心模块（`workflow_engine` `tool_registry` `agent_runtime` `auth` `vision.rules`）≥70%，整体 ≥50%
- CI：`ruff check` + `pytest --cov` + `npm run build`；本地 pre-commit 钩子同款
- 性能脚本：`bench_event_latency.py`（事件端到端 p50/p95）、`bench_concurrent_runs.py`（10 并发工作流）

---

## 14. 阶段与验收

| Phase | 内容 | 验收命令 / 证据 | 估时 |
|---|---|---|---|
| **P1 基础** | 脚手架、compose、PG/Redis/MQTT、用户认证、Alembic 首版迁移、统一响应与异常、`/metrics` | `docker compose --profile core up -d` 后能登录；`pytest` 绿；`/docs` 可用 | 3d |
| **P2 Agent 管理** | Agent/模型/工具 CRUD、Tool Registry、单 Agent 试跑、运行记录 | 建一个 Agent 绑 1 个工具，`POST /agents/{id}/test` 返回 agent_run 且含 tool_calls | 4d |
| **P3 多 Agent** | 6 个 Agent + Supervisor 编排 + 结构化输出 + 证据链 | 一次请求返回报告，`agent_runs` 里有 6 条记录，报告每条结论带事件 ID | 5d |
| **P4 边缘与设备** | edge-agent、MQTT、2 个模拟节点、设备探测、断线重连 | `python -m edge_agent.main --simulate --nodes 2` 后 `/nodes` 显示 2 在线；杀掉再起，心跳恢复 | 4d |
| **P5 视觉与 RAG** | MockDetector、规则、真截图、事件入库、知识库入库与混合检索 | `scripts/seed_demo.py` 后 `/events` 有 200 条；`/knowledge/search` 返回带 chunk_id | 4d |
| **P6 工作流与可视化** | Vue Flow 编辑器、发布、执行引擎、审批门、实时进度 | 界面拖出「start→vision→condition→approval→report→end」并成功执行 + 审批恢复 | 6d |
| **P7 监控与部署** | Prometheus/Grafana profile、日志追踪、离线包、演示数据、README/架构图/ER 图/演示脚本 | `docker save` 后断网 `compose up` 跑通核心流程；`docs/eval.md` 有 3 个指标实测 | 3d |

合计约 **29 个工作日**。

### 14.1 三个量化指标（交付时必须实测）

1. 事件端到端延迟（agent 上报 → 落库 → WS 推送）p95 < 1.5s（局域网，2 节点 4 路 5fps）
2. 6 Agent 一次完整协作 p95 < 25s（Mock LLM 下 < 3s，需分别标注）
3. 断网 5 分钟事件完整率 100%、重复率 0

---

## 15. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| `conda --clone` 体积大 / 慢 | 环境起不来 | 先测 clone；失败则新建 env + 装 CPU torch（本机 langchain_env 有 2.6.0+cpu 可参考） |
| `lawApp_LangGraph` 的 torch 2.12.0+cpu 版本异常 | sentence-transformers 报错 | 固定版本写进 `requirements.txt`；必要时降到 torch 2.6.0+cpu |
| E 盘只剩 17G | 镜像/截图写满 | Docker 数据根迁到 F；截图按天分目录 + 90 天清理；镜像只保留 CPU 版 |
| RTSP 在本机无法解码 | 演示受阻 | 演示默认走 `file://` 视频 + MockDetector；RTSP 只做探测与可选接入 |
| DeepSeek key 失效 / 断网 | Agent 演示失败 | `LLM_MODE=auto` 自动降级 mock；CI 一律 mock |
| 中文全文检索无 zhparser | 混合检索退化为向量为主 | 用 `simple` 配置 + `pg_trgm`，并在文档中说明局限 |
| 6 Agent 编排易失控（循环/超预算） | 费用与延迟 | Supervisor 硬限制：最大 6 次 Agent 调用、总 token 上限、总时长 60s，超限返回部分结果 |
| 审批节点长期挂起 | run 堆积 | 默认 24h 超时；UI 有待审批角标 |

---

## 16. 二期（不在本期）

`roles/permissions` 表与多租户隔离、`edge_node_metrics` 时序表、`alarm_events` 独立表、YOLO 真推理、GPU torch、K8s 多节点、Grafana 告警规则、Agent 自进化/评测闭环。

---

## 17. 交付物清单

1. 后端（api + worker + 6 Agent + Tool Registry + RAG + MQ）2. 前端（10 个页面）3. edge-agent（含模拟模式）4. mcp-server
5. `docker-compose.yml` + profiles 6. Alembic 迁移（16 表）7. ER 图 + 架构图（Mermaid）
8. `README.md`（含 5 分钟跑通）9. `docs/eval.md`（实测指标）10. `scripts/`（seed / bench / retention）
11. 测试报告 12. 3 分钟演示脚本
