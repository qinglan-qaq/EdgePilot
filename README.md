# EdgePilot

面向工业视觉、IoT 与边缘计算场景的统一 AI Agent 管理、工作流编排与可观测平台。
本仓库交付 P0–P7：事件接入、六类 Agent 协作、工作流 DSL/审批、Prompt 版本管理、
Trace/Span 全链路追踪、LLM/工具/RAG 观测、离线部署与数据保留。

> 所有演示事件、截图与 Mock LLM 输出均显式标记为模拟；不访问外网、不控制真实设备。

## 5 分钟跑通

前置：Docker Desktop（Linux 容器）、Python 3.11（安装
`backend/requirements.txt`）、Node.js 22 + npm。

```bash
# 1) 启动基础服务并迁移
cp -n docker/compose/.env.example docker/compose/.env
docker compose -f docker/compose/docker-compose.yml --profile app up -d \
  postgres redis rabbitmq mosquitto
docker compose -f docker/compose/docker-compose.yml --profile app run --rm api \
  alembic upgrade head
docker compose -f docker/compose/docker-compose.yml --profile app up -d --build

# 2) 初始化演示账号与数据（幂等）
export SEED_PASSWORD='edgepilot-demo'
python backend/scripts/seed_users.py
python backend/scripts/seed_demo.py
python backend/scripts/seed_workflow_demo.py --run

# 3) 启动前端
cd web && npm ci && npm run dev
```

打开 http://127.0.0.1:5173 ，使用 `admin@edgepilot.local` /
`edgepilot-demo` 登录。API 文档在 http://127.0.0.1:8000/docs 。

首次 `npm ci` 与镜像构建会超过 5 分钟；已缓存依赖时按上述步骤可直接跑通。
Windows Git Bash 下若 `make` 不可用，使用上面的 compose 命令即可。

### 快速验证

- 「工作流」→「园区周界巡检（演示）」：查看 DSL、校验、版本；
- 「运行追踪」：打开 seed 产生的运行，审批/查看报告与节点耗时；
- 「AI Observability → Trace 查询」：打开 trace，查看 span 树与 10 个 Inspector
  Tab（LLM/工具/检索/事件/错误/原始 JSON 等）；
- 「AI Observability → 概览」：指标卡与趋势；成本缺失时显示覆盖比例；
- 「提示词 → Playground」：变量渲染、版本对比、执行记录。

## 功能范围

| 领域 | 已交付 |
|---|---|
| 平台底座 | FastAPI 模块化单体、Celery worker/beat、Postgres+pgvector、Redis、RabbitMQ、Mosquitto |
| 多租户与权限 | admin/operator/viewer、JWT + refresh、租户隔离（跨租户 404）、审计日志 |
| 边缘接入 | 节点注册/5s 心跳/离线扫描、设备、事件、MQTT 下行命令、SQLite outbox 断网补发 |
| Agent | Supervisor + device/vision/diagnosis/operations/report 六类角色、发布版本、试跑 |
| 工作流 | DSL 校验、草稿/发布不可变版本、条件/审批/结束节点、Celery 执行与审批恢复 |
| 知识库 | 文档解析/分块、pgvector + 全文/trigram 混合检索、重排 |
| 可观测 | Trace/Span/Event、SSE、10 个 Inspector Tab、8 个观测页面、Analytics SQL 聚合 |
| LLM/工具/RAG 观测 | token/耗时/成本（缺失即不可用）、工具权限/重试/错误、检索记录、LangGraph 回调 |
| Prompt | 模板/不可变版本、发布/回滚、Playground、版本对比、执行记录 |
| 运维 | Prometheus `/metrics`、Grafana 看板（observability profile）、保留清理、离线包 |
| 安全 | 密钥 Fernet 加密、日志脱敏、采集模式 OFF/METADATA/REDACTED/FULL、观测故障隔离 |

## 架构

```mermaid
flowchart LR
    Edge[edge-agent] --> MQTT[(Mosquitto)]
    MQTT --> API[FastAPI :8000]
    API --> PG[(Postgres :5433)]
    API --> Redis[(Redis)]
    API --> MQ[(RabbitMQ)]
    MQ --> Worker[Celery worker]
    Web[Vue 3] --> API
    Prom[Prometheus] --> API
    Grafana --> Prom
```

- 分层：`api → services → repositories → models`；路由不直接写 SQL。
- 观测链路：`Tracer.emit → 脱敏 → EventCollector 批量 → BatchWriter`；
  写库失败只告警，不影响业务返回。
- `trace_id` 为 W3C 32 位十六进制，`span_id` 为 16 位；ID 只做关联，
  页面展示的是实际执行时间与内容。

详见 [架构说明](./docs/architecture.md) 与 [数据模型](./docs/er.md)。

## 测试

```bash
cd backend && python -m pytest -q          # 481 passed
cd edge-agent && python -m pytest -q       # 8 passed
cd web && npm test                          # 84 passed
cd web && npm run build                    # 类型检查 + 产物
```

交付指标（实测，Mock 与真实 LLM 分开标注）：

| 指标 | 实测 | 目标 |
|---|---|---|
| 事件端到端 p95 | 6.11ms | < 1.5s |
| 6 Agent 协作 p95（Mock） | 154.43ms | < 3s |
| 6 Agent 协作 p95（真实 LLM） | 未测（无 Key） | < 25s |
| 断网 5 分钟事件完整率 | 100%，重复 0 | 100%，重复 0 |

复现命令与限制见 [评测报告](./docs/eval.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [architecture.md](./docs/architecture.md) | 组件、分层、数据流、跨进程追踪、部署形态 |
| [er.md](./docs/er.md) | 当前 25 张表（第 26 张评测表属 P8）、约束/索引、迁移链、保留策略 |
| [api.md](./docs/api.md) | 全部 `/api/v1` 路由、权限、信封、错误码 |
| [runbook.md](./docs/runbook.md) | 启停、迁移、seed、日志、备份、故障排查 |
| [offline-install.md](./docs/offline-install.md) | 离线包生成、校验、导入、升级/回滚 |
| [eval.md](./docs/eval.md) | 三项指标实测、质量门禁、未测边界 |
| [demo-script.md](./docs/demo-script.md) | 10–15 分钟演示流程与讲解边界 |
| [docs/plans](./docs/plans) / [docs/specs](./docs/specs) | 原始需求、设计与实施计划 |

## 配置与安全

- 生产必须覆盖 `JWT_SECRET`、`POSTGRES_PASSWORD`、`SECRET_ENCRYPTION_KEY`、
  `GRAFANA_ADMIN_PASSWORD`；示例值仅用于本地。
- 模型密钥只存 `api_key_enc`（Fernet），API 响应与日志不回显明文。
- 生产默认 `OBS_CAPTURE_MODE=REDACTED`；查看完整 prompt/工具参数需管理员权限
  且写审计。
- provider 未返回 token/成本时标记“不可用”，绝不估算。
- 客户端不直连数据库；所有访问经 `/api/v1`。
- MQTT 默认匿名仅适合本地；生产需启用认证/TLS。

## 已知边界 / 后续（P8）

- 不做 Replay 与评测闭环（实验管理、LLM judge、标注）。
- 不做 YOLO 真实推理、GPU torch、K8s 多节点调度。
- 前端「概览/事件中心/节点/设备/知识库/系统设置」仍是占位页；对应后端 API
  已就绪（Dashboard、Events、Nodes、Devices、Knowledge、Audit、Users）。
- 大数据量下的 Trace 首屏/导出优化、运行级实时事件、证据 Markdown 自动定位
  留待后续迭代。
