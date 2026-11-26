# 演示脚本（15–20 分钟）

演示目标：展示「视觉事件/设备 → 工作流审批 → Agent 报告 → 全链路 Trace」闭环，
以及真实视频推理、Prompt 版本、工具/模型/Agent 观测与审计。模拟事件与 Mock LLM
输出均显式标记；视觉框与置信度来自真实 YOLO11n 推理，不使用 Mock 检测结果。

## 0. 准备

```bash
export SEED_PASSWORD='edgepilot-demo'
python backend/scripts/seed_users.py
python backend/scripts/seed_demo.py
python backend/scripts/seed_workflow_demo.py --run

# 视觉演示（可选但推荐）
python backend/scripts/fetch_vision_sample.py   # OpenCV vtest.avi（不提交 git）
python backend/scripts/seed_vision_demo.py      # 幂等登记 yolo11n 许可元数据
docker compose -f docker/compose/docker-compose.yml \
  --profile app --profile vision up -d --build
# 权重被 gitignore，先下载到本地再复制进容器
cd vision-service && python scripts/fetch_model.py && cd ..
docker compose -f docker/compose/docker-compose.yml cp \
  vision-service/weights/yolo11n.pt vision:/app/weights/yolo11n.pt
curl -X POST -H "X-Vision-Token: $VISION_SERVICE_TOKEN" \
  http://127.0.0.1:8001/v1/models/reload
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

## 5. 视觉识别：真实视频推理（约 4 分钟）

1. **取真实视频**：`python backend/scripts/fetch_vision_sample.py` 下载 OpenCV 官方
   `vtest.avi` 到 `vision-service/var/samples/`；脚本打印 URL、字节数与 SHA-256。
2. **确认服务**：`GET /api/v1/vision/health` 的 `reachable=true`，推理服务
   `http://127.0.0.1:8001/health` 为 `ok`、`model.load_status=ready`；
   `/vision/models` 能看到 `yolo11n` 的真实版本（`yolo11n-<sha 前 12 位>`）。
3. **上传**：进入 `/vision/detect`，选择 `vtest.avi`，置信度保持默认 0.25，提交。
   说明后端先 probe 真实时长/帧率，再按目标帧数抽帧；同文件同参数重复上传会
   命中幂等任务，不重复推理。
4. **等待完成**：任务从 `pending` → `running` → `completed`；页面轮询进度。
   若失败，展示真实 `error_code/error_message` 与“重试”，不生成假结果。
5. **播放与目标框**：在播放器中拖动时间轴，叠加框来自该帧真实检测结果；
   当前帧目标列表显示类别、置信度与 bbox。强调：**框和置信度是真实推理输出**，
   不是演示占位图。
6. **任务详情**：打开 `/vision/records/:taskUid`，查看帧数、检测数、事件数、
   模型版本与耗时；可切到结果过滤（类别/置信度/时间窗）。
7. **触发研判**：点击“Agent 研判”，展示研判结论与 trace 链接；同任务成功后
   复用既有运行。说明研判文本来自 LLM（默认 Mock 夹具），而框来自真实推理，
   两者边界分开讲。
8. **若视觉服务被停掉**：EdgePilot 其它模块照常可用；`/vision/*` 明确显示
   推理服务不可达/降级，新任务失败并提示重试，已完成任务的记录、事件与视频
   （后端签名流）仍可查询。不要用 Mock 输出替代。

## 6. Prompt 版本与 Playground（2 分钟）

1. 进入「提示词」，打开任一模板；展示活动版本、版本列表、changelog。
2. 新建一个版本（只追加），发布后活动版本指针移动；回滚会生成新版本，
   历史版本保持不变。
3. 打开 Playground：填写变量，渲染 system/user prompt；对比两个版本或两种
   模型输出；说明模板变量缺失会明确报错，不静默填空。
4. 查看执行记录：模板版本、渲染哈希、trace/span 关联。

## 7. 工具、模型、Agent 分析（2 分钟）

1. 「工具分析」：调用次数、成功率、p95 耗时、错误；区分权限拒绝与执行失败。
2. 「模型分析」：按模型统计请求、token、延迟；缺价模型单独标注。
3. 「Agent 分析」：Agent 维度运行数、失败率、token/cost。
4. 「可观测设置」（admin）：只读展示采集模式、采样率、批量与保留策略；
   生产默认 `REDACTED`。

## 8. API/审计补充（1 分钟，可选）

- `GET /api/v1/dashboard/summary`：节点/设备/事件/运行计数与 24h 趋势。
- `GET /api/v1/events?severity=high`：事件列表；`POST /events/{id}/ack` 写审计。
- `GET /api/v1/audit-logs`（admin）：发布、审批、工具执行、命令下发等操作。
- 说明前端事件/节点/设备页当前仍是占位页，API 已可用；这是本期已知范围，
  不作为已完成 UI 演示。

## 9. 离线与保留（1 分钟，可选）

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

## 10. 讲解边界（必须说明）

- MQTT 上报的视觉事件与截图是演示/模拟数据；Vision Center 上传视频的框与置信度
  来自真实 YOLO11n 推理，两类数据源不同，不混用、不互相替代；
- 视觉只做预训练 YOLO11n 推理，不做训练/微调/优化/导出；
- Mock LLM 输出是确定性夹具，不代表真实模型能力；真实 LLM 延迟未测；
- YOLO11n 为 AGPL-3.0：可遵守 AGPL 或购买 Ultralytics Enterprise License，
  不默认授予商业使用权限；
- 未实现 Replay 与评测闭环（P8）；
- 前端事件中心/节点/设备/知识库页面为占位页，对应 API 已就绪。
