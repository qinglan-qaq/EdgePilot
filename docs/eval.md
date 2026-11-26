# 评测与实测指标

本文记录 P7 交付时的实测结果与复现方法。所有数字来自本仓库脚本的真实运行，
不使用估算值；Mock LLM 与真实 LLM 分开标注。

## 1. 测试环境

| 项 | 值 |
|---|---|
| 日期 | 2026-11-26 |
| 宿主 | Windows 11（10.0.26200），Docker Desktop（Linux 容器） |
| API | `edgepilot-api:local` 容器，Uvicorn，`OBS_ENABLED=true`、`OBS_CAPTURE_MODE=REDACTED` |
| 数据库 | PostgreSQL 16 + pgvector（compose 端口 5433） |
| Broker | Mosquitto 2（1883）、RabbitMQ 3.13、Redis 7 |
| LLM | `LLM_MODE=mock`，确定性夹具；无真实 API Key、无外网调用 |
| 数据量 | seed_demo：2 节点、4 相机、200 条模拟事件；基准事件独立写入后清理 |

## 2. 三项交付指标

| # | 指标 | 口径 | 目标 | 实测 | 结论 |
|---|---|---|---|---|---|
| 1 | 事件端到端延迟 p95 | MQTT publish → `vision_events.received_at` | < 1.5s | **6.11ms** | 通过 |
| 2 | 6 Agent 协作 p95（Mock） | `POST /agents/{supervisor}/test` 完整响应，20 次 | Mock < 3s | **154.43ms** | 通过 |
| 2b | 6 Agent 协作 p95（真实 LLM） | 同上，真实 provider | < 25s | **未测** | 无真实 Key，禁止用 Mock 数字替代 |
| 3 | 断网 5 分钟事件完整率 | Outbox 断网缓冲 → 恢复补发，2 节点 × 5fps × 300s | 100%，重复 0 | **100.00%，重复 0** | 通过 |

> 说明：指标 1 统计到服务端落库，不含浏览器 SSE 渲染；SSE 只读已落库数据。
> 指标 3 是进程内等效演练（真实 `Outbox` SQLite 实现，broker 不可达），
> 不是物理断网 5 分钟的墙钟测试，详见 §5。

## 3. 指标 1：事件端到端延迟

命令：

```bash
python scripts/bench_event_latency.py --count 100 --rate 20
```

结果：

```json
{
  "count": 100,
  "rate_per_s": 20.0,
  "p50_ms": 3.974,
  "p95_ms": 6.110,
  "p99_ms": 7.379,
  "min_ms": 3.405,
  "max_ms": 8.849,
  "missing": 0,
  "method": "MQTT publish -> vision_events.received_at"
}
```

解读：

- 100 条 QoS1 事件以 20 条/秒写入，全部落库，无丢失；
- p95 远低于 1.5s 目标；本条链路没有真实视觉推理，仅验证控制面接入与写库；
- 脚本默认创建 `bench-node-*` 临时节点并在结束后删除，事件带
  `simulated=true`。

## 4. 指标 2：6 Agent 协作延迟（Mock）

命令：

```bash
export EDGEPILOT_TOKEN='<admin access token>'
python scripts/bench_six_agents.py --runs 20 --cleanup
```

结果（Mock LLM，20 次，全部 `succeeded`）：

```json
{
  "mode": "mock",
  "runs": 20,
  "agent_count": 6,
  "p50_ms": 85.363,
  "p95_ms": 154.432,
  "p99_ms": 1003.493,
  "min_ms": 70.036,
  "max_ms": 1003.493,
  "statuses": ["succeeded"]
}
```

解读：

- 一次运行包含 Supervisor planner + device/vision/diagnosis/operations/report
  五个子 Agent，响应中 `assignments` 恰好 5 条；
- p95 154ms，低于 Mock 目标 3s；最大值约 1s 属于首次/抖动离群值；
- 这是 Mock 夹具下的编排与持久化开销，**不能代表真实 LLM 延迟**。

真实 LLM：

- 当前环境没有 provider API Key，`LLM_MODE=mock`，因此未测；
- 真实模式需在模型配置中填 `api_key`、单价，并将 `LLM_MODE=real`，
  再以相同脚本/相同运行次数测量；目标 < 25s；
- provider 不返回 usage/单价时，成本字段保持“不可用”，不得估算。

## 5. 指标 3：断网 5 分钟事件完整率

命令：

```bash
python scripts/bench_offline_outbox.py
```

结果：

```json
{
  "mode": "outbox-equivalent",
  "outage_seconds": 300,
  "nodes": 2,
  "fps_per_node": 5.0,
  "expected": 3000,
  "delivered": 3000,
  "unique_delivered": 3000,
  "pending_after_flush": 0,
  "completeness_pct": 100.0,
  "duplicate_count": 0,
  "duplicate_rate_pct": 0.0
}
```

口径与限制：

- 使用 edge-agent 的真实 `Outbox`（SQLite + WAL）与 `MqttClient.publish`
  离线分支：broker 不可达时 3,000 条事件全部入队；
- 恢复后循环 `flush`（每批 100）直到排空，按 `msg_id` 统计唯一投递；
- 这是**等效演练**，验证缓冲/补发/去重语义；没有真实暂停网络 5 分钟。
  真实验收可在目标环境 `docker stop` Mosquitto 5 分钟后恢复，再用
  `vision_events` 计数复核；
- 服务端 `(tenant_id, dedup_key)` 唯一约束与 MQTT `msg_id` 去重是重复率为 0
  的第二道保障；本演练未重放已 ack 消息。

## 6. 质量门禁

| 套件 | 命令 | 结果 |
|---|---|---|
| 后端 | `cd backend && python -m pytest -q` | **481 passed**（含 e2e） |
| edge-agent | `cd edge-agent && python -m pytest -q` | **8 passed** |
| 前端 | `cd web && npm test` | **84 passed**（15 个文件） |
| 前端类型/构建 | `cd web && npm run build` | 通过；主包约 1.6MB，有 chunk 体积警告 |
| composes | `docker compose ... config -q` | 通过（基础栈与观测 overlay） |

## 7. 离线包检查（补充）

`bash scripts/offline_pack.sh` 实测：

- 5 个核心镜像，`images.tar` 382,905,344 字节；
- `web-dist.tar.gz` 565,720 字节；
- 20 个文件写入 `SHA256SUMS`，`sha256sum -c` 全部 OK；
- `manifest.json` 使用包内相对路径键（`docker/compose/docker-compose.yml` 等）；
- `bash scripts/offline_pack.sh --load dist/offline/images.tar` 校验并导入成功；
- 包内 compose `config -q` 通过，说明相对挂载路径完整。

## 8. 未测项与边界

- 真实 LLM 延迟/成本、真实摄像头推理：无 Key/无设备，明确未测。
- 观测写入单批延迟（spec 目标 80ms）、Trace 详情首屏 300ms 等未单独压测；
  当前只有单元/集成测试与功能验收，不把它们写成“已达标”。
- 大规模并发（10 工作流并发）未在本机压测；`bench_concurrent_runs.py`
  不在本期交付范围。
- Replay、RAG 评测、LLM judge 属于 P8，不在本报告范围。

## 9. 复现注意事项

- 基准脚本依赖本机 Docker 栈；先确认 `api` 健康、Postgres 5433 可达；
- `bench_six_agents.py` 需要 admin token（15 分钟有效），并在
  `backend/demo/fixtures/llm/` 写夹具；compose 只读挂载不影响宿主写入；
- 6 Agent 基准会创建 Agent 与运行记录；`--cleanup` 只能把 Agent 置为
  disabled（存在运行记录时 API 禁止硬删除），夹具会删除；
- 所有基准数据显式标记 simulated，不进入演示结论。
