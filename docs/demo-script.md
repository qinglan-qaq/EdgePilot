# 演示脚本（10–15 分钟）

演示目标：展示「视觉事件/设备 → 工作流审批 → Agent 报告 → 全链路 Trace」闭环，
以及 Prompt 版本、工具/模型/Agent 观测与审计。所有数据均为本地 Mock/模拟，
不访问外网、不控制真实设备。

## 0. 准备

```bash
export SEED_PASSWORD='edgepilot-demo'
python backend/scripts/seed_users.py
python backend/scripts/seed_demo.py
python backend/scripts/seed_workflow_demo.py --run
cd web && npm run dev
```

- UI：http://127.0.0.1:5173
- API 文档：http://127.0.0.1:8000/docs
- 账号：`admin@edgepilot.local` / `operator@edgepilot.local` /
  `viewer@edgepilot.local`，密码均为 `SEED_PASSWORD`
- `seed_workflow_demo.py --run` 会以 operator 触发、审批一次运行，并校验
  trace；如果不需要预跑，去掉 `--run`。

## 1. 登录与权限（1 分钟）

1. 打开 http://127.0.0.1:5173 ，用 admin 登录。
2. 侧边栏可见：工作流、运行追踪、AI Observability；系统设置/概览等模块
   当前为占位页，不在本次演示范围。
3. 说明三种角色：admin 可发布/试跑；operator 可触发/审批；viewer 只读。

## 2. 工作流列表与编辑器（2 分钟）

1. 进入「工作流」，打开「园区周界巡检（演示）」。
2. 编辑器展示 6 个节点：start → vision → condition → approval → report →
   end_report，另有 end_insufficient 分支。
3. 点击「校验」，展示 DSL 校验通过；校验会检查节点类型、边、Agent 引用。
4. 说明版本语义：草稿为 v0，发布后生成不可变版本；发布/回滚不会改写历史。

## 3. 触发运行与审批（2 分钟）

1. 点击「试跑/运行」，输入任务（默认即可），运行进入 `pending/running`。
2. 运行停在 approval 节点，状态为 `waiting_approval`。
3. 切到 operator 账号（或使用 `seed_workflow_demo.py --run` 已完成的运行），
   在「运行追踪」打开运行详情，点击「通过」。
4. Celery worker 恢复执行，report Agent 生成 Markdown 报告；运行变为
   `succeeded`。
5. 运行详情中查看：
   - 节点状态与耗时；
   - 报告结论与编号证据；每条证据能定位到事件/指标；
   - trace_id 与 span 入口。

## 4. Trace Detail（3 分钟）

1. 进入「AI Observability → Trace 查询」，按来源/状态/Agent 过滤；也可按
   trace_id 搜索。
2. 打开一次运行对应的 Trace：
   - 左栏：trace 汇总（耗时、token、cost、span/error 数）；
   - 中栏：span 树（workflow → agent/node → llm/tool/retrieval）；
   - 右栏：Inspector 10 个 Tab，包括概览、输入输出、LLM 调用、工具调用、
     检索、事件、错误、原始 JSON 等。
3. 说明 `trace_id`/`span_id` 只做关联；执行时间看 `start_time`/`duration_ms`，
   不从 ID 解析时间。
4. 在 LLM Tab 指出：Mock 没有真实 usage/单价时，token/cost 显示“不可用”，
   不显示 0 或估算值。
5. 打开「可观测概览」：指标卡与趋势图；每张卡 tooltip 标注口径。成本卡若有
   缺价模型，会显示覆盖比例而不是伪造总额。
6. 可选：打开 SSE 实时流，展示同一 trace 的在线更新。

## 5. Prompt 版本与 Playground（2 分钟）

1. 进入「提示词」，打开任一模板；展示活动版本、版本列表、changelog。
2. 新建一个版本（只追加），发布后活动版本指针移动；回滚会生成新版本，
   历史版本保持不变。
3. 打开 Playground：填写变量，渲染 system/user prompt；对比两个版本或两种
   模型输出；说明模板变量缺失会明确报错，不静默填空。
4. 查看执行记录：模板版本、渲染哈希、trace/span 关联。

## 6. 工具、模型、Agent 分析（2 分钟）

1. 「工具分析」：调用次数、成功率、p95 耗时、错误；区分权限拒绝与执行失败。
2. 「模型分析」：按模型统计请求、token、延迟；缺价模型单独标注。
3. 「Agent 分析」：Agent 维度运行数、失败率、token/cost。
4. 「可观测设置」（admin）：只读展示采集模式、采样率、批量与保留策略；
   生产默认 `REDACTED`。

## 7. API/审计补充（1 分钟，可选）

- `GET /api/v1/dashboard/summary`：节点/设备/事件/运行计数与 24h 趋势。
- `GET /api/v1/events?severity=high`：事件列表；`POST /events/{id}/ack` 写审计。
- `GET /api/v1/audit-logs`（admin）：发布、审批、工具执行、命令下发等操作。
- 说明前端事件/节点/设备页当前仍是占位页，API 已可用；这是本期已知范围，
  不作为已完成 UI 演示。

## 8. 离线与保留（1 分钟，可选）

```bash
# 观测栈
docker compose -f docker/compose/docker-compose.yml \
  -f docker/compose/docker-compose.observability.yml \
  --profile app --profile observability up -d
# Grafana http://127.0.0.1:3000 ，看板 EdgePilot Overview

# 保留清理：先 dry-run
docker compose -f docker/compose/docker-compose.yml --profile app exec api \
  python /app/scripts/retention.py --dry-run
```

说明：

- 离线包 `dist/offline` 可在无网目标机 `docker load` 后启动；
- 断网 5 分钟由 edge-agent SQLite outbox 缓冲，恢复后按 seq 补发，服务端
  按 `(tenant_id, dedup_key)` 去重；
- 实测指标见 [eval.md](./eval.md)。

## 9. 讲解边界（必须说明）

- 视觉事件与截图是演示/模拟数据，不是真实检测结论；
- Mock LLM 输出是确定性夹具，不代表真实模型能力；真实 LLM 延迟未测；
- 未实现 Replay 与评测闭环（P8）；
- 前端事件中心/节点/设备/知识库页面为占位页，对应 API 已就绪。
