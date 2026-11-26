# EdgePilot 视觉中心（Vision Center）交付说明

- 交付日期：2026-11-26
- 交付范围：`e40d705` 起的 17 个提交（含终审修复批次）中的 Vision Center 全部功能、测试、文档与 Docker 集成；本文档与 `docs/evidence/vision/` 为随后的交付提交。
- 设计依据：[spec](./2026-11-26-edgepilot-vision-spec.md)、[design](./2026-11-26-edgepilot-vision-design.md)、[plan](./2026-11-26-edgepilot-vision-plan.md)。
- 结论：P1–P6 全部落地，真实视频 `vtest.avi` 经真实 YOLO11n CPU 推理跑通「上传 → 推理 → 结果落盘 → 播放器叠框 → 事件 → 工作流 / Agent 研判」闭环；全量测试 **870 通过 / 0 失败**。

## 0. 交付结论速览

| 项 | 结论 |
| --- | --- |
| 真实推理 | 795 帧、6,790 次检测、person 4,618，最高置信度 0.9168；证据见第 8 节 |
| 端到端联调 | Docker Compose 实跑：任务 completed、16 个事件、工作流 Run #7 succeeded、AgentRun #253 succeeded |
| 测试 | 后端 701、推理服务 38、前端 131，合计 870 全部通过；`vue-tsc` 无错误 |
| 隔离性 | 关闭或停止 vision 不影响登录、Agent、工作流、设备、事件等既有模块 |
| 既有模块 | 未改动既有 Agent/工作流/设备/事件中心行为；`POST /agents/{id}/test` 仍仅 admin |
| 许可 | AGPL-3.0；AGPL 合规使用或 Ultralytics Enterprise License 二选一，不默认授予商业使用权 |

## 1. 新增/修改文件清单

相对 Vision 工作起点 `d5a0bd1`，共 **97 个受版本控制文件**（新增 73 / 修改 24），另有 9 个证据文件与本文档。

### 1.1 后端应用（21）

| 类别 | 文件 |
| --- | --- |
| API | `backend/app/api/v1/vision.py`（新增，14 路由）、`backend/app/api/v1/router.py`（挂载） |
| 配置/入口 | `backend/app/core/config.py`（25 个 `VISION_*` 配置）、`backend/app/core/exceptions.py`（Conflict→409）、`backend/app/main.py`（轮询器 lifespan、关闭时停轮询） |
| 模型 | `backend/app/models/vision.py`（3 表）、`backend/app/models/__init__.py` |
| 仓储 | `backend/app/repositories/vision_repo.py`、`backend/app/repositories/workflow_repo.py`（按事件类型/载荷查找运行） |
| 契约 | `backend/app/schemas/vision.py` |
| 服务 | `vision_task_service.py`（上传/指纹/派发/重试/取消）、`vision_storage.py`（落盘/哈希/原子写）、`vision_client.py`（推理服务 HTTP 客户端）、`vision_sync.py`（轮询/退避/恢复）、`vision_result_service.py`（artifact 入库）、`vision_event_service.py`（事件候选/去重/冷却）、`vision_trigger_service.py`（工作流幂等触发）、`vision_agent_service.py`（任务级研判）、`vision_media.py`（HMAC 签名 URL）、`vision_cleanup_service.py`（保留/清理审计） |
| 环境样例 | `backend/.env.example`（`VISION_*` 配置样例） |

### 1.2 数据迁移（1）

- `backend/migrations/versions/0006_vision.py`（`down_revision = 0005_obs_analytics_indexes`）。

### 1.3 运维脚本（4）

- `backend/scripts/fetch_vision_sample.py`：下载 `vtest.avi`，校验魔数/大小/SHA，`.part` + `os.replace`。
- `backend/scripts/seed_vision_demo.py`：幂等登记模型元数据与许可提示（不生成任务或结果）。
- `backend/scripts/seed_workflow_demo.py`：新增「视觉事件研判（演示）」触发工作流。
- `backend/scripts/vision_cleanup.py`：清理 CLI，默认删除文件、保留 PG 摘要与审计。

### 1.4 后端测试（17）

- 集成（10）：`test_vision_acceptance.py`、`test_vision_resilience.py`、`test_vision_api.py`、`test_vision_upload.py`、`test_vision_completion.py`、`test_vision_trigger.py`、`test_vision_agent.py`、`test_vision_sync.py`、`test_vision_linkage.py`、`test_vision_cleanup.py`。
- 单元（5）：`test_vision_storage.py`、`test_vision_media.py`、`test_vision_client.py`、`test_vision_schemas.py`、`test_vision_rules_events.py`。
- 夹具（2）：`backend/tests/conftest.py`、`backend/tests/integration/test_schema_manifest.py`（表清单与迁移对齐）。

### 1.5 推理服务 vision-service（20）

| 类别 | 文件 |
| --- | --- |
| 应用（9） | `app/__init__.py`、`app/main.py`、`app/config.py`、`app/auth.py`、`app/model.py`、`app/inference.py`、`app/jobs.py`、`app/standardize.py`、`app/schemas.py` |
| 依赖/脚本（3） | `pyproject.toml`、`requirements.txt`、`scripts/fetch_model.py` |
| 测试（8） | `tests/conftest.py`、`tests/fakes.py`、`tests/test_api.py`、`tests/test_jobs.py`、`tests/test_config_model.py`、`tests/test_standardize.py`、`tests/test_inference_units.py`、`tests/test_inference_real.py` |

### 1.6 前端（18）

- 源码（13）：`web/src/api/vision.ts`、`web/src/stores/vision.ts`、`web/src/utils/vision.ts`、`web/src/components/vision/VisionPlayer.vue`、`web/src/components/vision/DetectionPanel.vue`、`web/src/views/vision/{VisionLayout,Overview,Detect,Records,TaskDetail,Models}.vue`、`web/src/router/index.ts`、`web/src/layouts/AppLayout.vue`。
- 测试（5）：`web/tests/test_vision_api.spec.ts`、`test_vision_store.spec.ts`、`test_vision_utils.spec.ts`、`test_vision_player.spec.ts`、`test_vision_pages.spec.ts`。

### 1.7 Docker 与仓库配置（5）

- `docker/vision.Dockerfile`（新增，CPU torch + ultralytics，不内置权重）。
- `docker/compose/docker-compose.yml`（`vision` profile、共享卷、api 的 `VISION_*` 环境）。
- `docker/compose/.env.example`（`VISION_SERVICE_TOKEN`）。
- `.dockerignore`（排除 `**/.venv/`、`**/var/`）、`.gitignore`（忽略权重、样例视频、运行产物）。

### 1.8 文档（11）

- 新增：spec / design / plan 三份（`docs/specs/2026-11-26-edgepilot-vision-*.md`）与本文档。
- 更新：`README.md`、`docs/api.md`、`docs/architecture.md`、`docs/demo-script.md`、`docs/er.md`、`docs/offline-install.md`、`docs/runbook.md`、`docs/user-flows.md`。

### 1.9 验收证据（9，本次提交）

- `docs/evidence/vision/README.md`、`docs/evidence/vision/frame-138.png`。
- `docs/evidence/vision/live-run/` 下 7 个真实响应片段（任务/结果/事件/工作流/研判/trace/重复上传）。

## 2. 架构与数据流

### 2.1 组件与职责

| 组件 | 职责 |
| --- | --- |
| Vue 3 前端 | 上传、列表/详情、播放器叠框、事件展示、模型许可页；2 秒轮询任务状态 |
| FastAPI 后端 | 权限与租户隔离、上传校验、指纹幂等、任务状态机、轮询与恢复、事件生成、工作流触发、Agent 研判、签名视频 |
| PostgreSQL | 任务汇总、结果摘要与 artifact 指针、模型登记、事件、工作流运行；**不存视频与帧级明细** |
| 共享存储 | `videos/<tenant>/<task_uid>.<ext>`、`jobs/<task_uid>/result.json`（artifact）、`jobs/<task_uid>/state.json`（推理服务状态） |
| vision-service | 预训练 YOLO11n 推理、抽帧、标准化 JSON、作业队列与状态持久化 |

### 2.2 主链路

```text
浏览器                       后端 API                     PostgreSQL                 vision-service
  │ POST /vision/tasks           │                            │                          │
  ├─────────────────────────────►│ 扩展名+魔数+大小校验         │                          │
  │                              ├─ /v1/probe ──────────────────────────────────────────►│
  │                              │  时长 > 900s → 422 video_too_long                     │
  │                              │  有效步长 = max(请求, ceil(帧数/900))，上限 2000 帧     │
  │                              │  指纹 = SHA256(租户+视频SHA+模型版本+置信度+采样+区域)  │
  │                              ├─ 查重/唯一约束 ─────────────►│                          │
  │                              ├─ 原子移动视频到共享存储        │                          │
  │                              ├─ 插入 vision_tasks ──────────►│                          │
  │                              ├─ /v1/jobs ──────────────────────────────────────────►│
  │                              │                            │        state.json 落盘    │
  │ 2s 轮询 GET /vision/tasks/{uid}│                            │                          │
  │◄─────────────────────────────┤ 轮询器（每任务独立会话）      │                          │
  │                              ├─ /v1/jobs/{uid} ────────────────────────────────────►│
  │                              │  成功 → /v1/jobs/{uid}/results                         │
  │                              │  vision-service 已写 artifact（tmp→os.replace）        │
  │                              ├─ 读取共享卷 artifact，校验字节 SHA/规范摘要/schema     │
  │                              ├─ 写 vision_results 后状态置 completed                  │
  │                              ├─ 幂等生成 vision_events ─────►│                          │
  │                              ├─ 按名称查已发布工作流并触发一次 ─►│ workflow_runs         │
  │                              │                            │                          │
  │ 播放器按 currentTime 匹配帧   │                            │                          │
  │◄── 签名 URL（1h，Range 206）─┤                            │                          │
```

### 2.3 关键设计

1. **视频与帧级结果不入库**：视频写入共享卷，帧级结果写 `result.json`；DB 只存 `artifact_path/bytes/sha256/schema_version` 与汇总。避免大对象拖垮 PostgreSQL，也让清理可以只删文件、保留业务摘要。
2. **结果先落盘、后置 completed**：vision-service 以 `tmp → os.replace` 原子写 artifact；后端在置 completed 前读取共享卷文件并校验字节 SHA-256、规范摘要与 `schema_version`，任一不符即置 failed。`COMPLETED` 不会指向未校验或缺帧的文件。结果行同时写入 `artifact_file_sha256`、`result_sha256`、`result_schema_version`。
3. **上传幂等**：`dedup_key = SHA256(tenant + 视频SHA-256 + 模型 key/version + 置信度 + 目标采样 + 步长 + 区域)`，配 `UNIQUE(tenant_id, dedup_key)`；并发上传靠唯一约束 + SAVEPOINT 收敛到同一任务，返回 `deduplicated=true`。
4. **任务状态可恢复**：推理服务把作业状态写 `state.json`；服务重启时未完成作业标记 `failed(service_restarted)`，已完成任务不受影响。后端轮询器启动时对账 `pending/running` 任务，超时 1800 秒、连续 30 次不可达、连续 3 次远端 404 分别收敛为明确错误码。
5. **事件幂等**：事件 `dedup_key = vision:<task_uid>:<event_type>:<frame_id>:<序号>`，配 5 秒冷却与数据库唯一约束；重复 ingest / 重放不新增事件。
6. **联动任务级一次**：同一视觉任务只允许一条 `trigger_type='event'` 的 `workflow_runs`（部分唯一索引 `uq_workflow_runs_vision_task`）；触发失败只写 `trigger_status/trigger_error`，**不改变已完成的推理状态**。Agent 研判同样按任务复用 `AgentRun`，不在帧级调用 LLM。
7. **视频访问最小化**：HMAC-SHA256 签名绑定租户、任务、视频 SHA，TTL 3600 秒，常量时间比较；`Range` 返回 206、越界 416、签名/归属不符统一 403 `invalid_media_token`；URL 不写日志。
8. **租户隔离**：全部查询带 `tenant_id` 条件；跨租户统一 404/403，不泄露资源是否存在。

### 2.4 与既有模块的边界

- `api` 不 `depends_on: vision`；vision 未启动时 `/vision/*` 返回明确降级结果，其他模块照常。
- `VISION_ENABLED=false` 时路由级统一 `503 vision_disabled`（包括无 Bearer 的视频路由）。
- 只新增视觉侧服务与路由，未改既有 Agent/工作流/设备/事件中心的业务逻辑；`POST /agents/{id}/test` 仍仅 admin，视觉直连研判允许 admin+operator。

## 3. 模型与版本

| 项 | 值 |
| --- | --- |
| 模型 | Ultralytics **YOLO11n** 预训练检测权重 `yolo11n.pt` |
| 固定版本 | 权重不随仓库分发；以 SHA-256 固定：`0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1` |
| 模型版本号 | `yolo11n-0ebbc80d4a76`（推理服务与 DB 一致） |
| 输入尺寸 | 640（`imgsz=640`） |
| 设备 | 默认 CPU（torch 2.5.1+cpu）；`VISION_DEVICE=cuda` 为可选配置，不是运行前提 |
| 能力边界 | 仅做检测：不训练、不微调、不优化、不导出 TensorRT/ONNX |
| 标准化 | Ultralytics 原始输出经 `standardize.py` 转成 `vision-result/v1`；业务层只消费统一 JSON，不接触原始对象 |
| 获取方式 | `cd vision-service && python scripts/fetch_model.py`（校验大小与 SHA，`.part` 原子落盘） |
| 许可证 | **AGPL-3.0** |

**许可与商业使用（双路径，均需自行落实）**：

1. 遵守 **AGPL-3.0** 义务使用（含对外提供网络服务时的源码提供义务等）；
2. 或向 Ultralytics 购买 **Enterprise License** 获得商业授权。

本仓库与模型登记均**不默认授予商业使用权限**；`/vision/models` 页面与 `vision_models.commercial_use_notice` 字段固定展示同一提示。闭源商用、对外服务或分发前请自行完成法务评估。

## 4. 新增 API 清单

### 4.1 后端（14，前缀 `/api/v1/vision`）

读接口任意登录角色；写接口 `admin/operator`；视频路由只认签名 token，不需要 `Authorization`。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/vision/health` | 登录 | 聚合推理服务与模型状态；不可达仍 200，`reachable=false` |
| GET | `/vision/overview` | 登录 | 任务/完成/运行/失败数、处理帧数、检测数、事件数、最近任务 |
| GET | `/vision/models` | 登录 | DB 登记 + 推理服务实时状态与许可事实 |
| GET | `/vision/tasks` | 登录 | 分页列表；`status/q/start/end` |
| GET | `/vision/tasks/{task_uid}` | 登录 | 详情、结果汇总、1 小时签名 `video_url` |
| GET | `/vision/tasks/{task_uid}/results` | 登录 | 帧级结果；`start_ms/end_ms/class_name/min_confidence/limit/offset`，`limit<=2000` |
| GET | `/vision/tasks/{task_uid}/events` | 登录 | 该任务的视觉事件（分页） |
| POST | `/vision/tasks` | admin/operator | multipart 上传；可选 `conf_threshold/frame_stride/target_samples/zones`；幂等返回 `deduplicated=true` |
| POST | `/vision/tasks/{task_uid}/cancel` | admin/operator | 终态幂等；推理服务不可达时仍在平台侧取消 |
| POST | `/vision/tasks/{task_uid}/retry` | admin/operator | 仅 `failed/cancelled`；重派同一任务，最多 3 次 |
| POST | `/vision/tasks/{task_uid}/trigger` | admin/operator | 受控重触发工作流；复用任务级幂等运行 |
| POST | `/vision/tasks/{task_uid}/analyze` | admin/operator | 用任务最新事件做 Agent 研判；同任务复用运行 |
| POST | `/vision/events/{event_id}/analyze` | admin/operator | 研判指定事件（归属其任务） |
| GET | `/vision/tasks/{task_uid}/video?token=…&expires=…` | 签名 | 视频流；Range 206 / 越界 416 / 签名不符 403 |

### 4.2 推理服务（8，默认 `127.0.0.1:8001`，可选 `X-Vision-Token`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务/队列/存储可写/模型加载状态 |
| GET | `/v1/models` | 模型名、版本、设备、类别、许可、`load_status` |
| POST | `/v1/models/reload` | 重新加载权重（复制权重进容器后必须调用） |
| POST | `/v1/probe` | 探测时长/帧率/分辨率 |
| POST | `/v1/jobs` | 创建推理任务，202 |
| GET | `/v1/jobs/{task_uid}` | 作业状态与进度 |
| POST | `/v1/jobs/{task_uid}/cancel` | 取消作业 |
| GET | `/v1/jobs/{task_uid}/results` | 读取 artifact；未完成 409，文件丢失 410 |

### 4.3 错误码

- 上传/查询 HTTP 原因：`invalid_file_type`、`invalid_zones`、`invalid_media_token`、`task_not_found`、`event_not_found`、`artifact_missing`、`video_missing`、`result_not_found`、`task_not_retryable`、`retry_limit_exceeded`、`trigger_pending`、`no_vision_events`、`vision_agent_missing`、`vision_agent_running`、`upload_conflict`、`file_too_large`、`video_too_long`、`artifact_unreadable`、`vision_disabled`、`artifact_corrupted`、`storage_write_failed`。
- 任务级 `error_code`：`video_missing`、`decode_failed`、`empty_video`、`model_unavailable`、`inference_failed`、`queue_full`、`service_shutting_down`、`timeout`、`cancelled`、`service_restarted`、`vision_job_lost`、`vision_service_unavailable`、`vision_failed`、`artifact_unreadable`、`purged`。
- 统一响应包络 `{code, message, data, trace_id}`；`code` 与 HTTP 状态一致，业务原因在 `data.reason`。

## 5. 数据库变更

迁移 `0006_vision` 只新增对象，不改既有业务表列：

### 5.1 新增 3 张表

| 表 | 关键字段 | 约束/索引 |
| --- | --- | --- |
| `vision_tasks` | `task_uid`、`dedup_key`、状态机、视频元数据、采样参数、模型与设备、错误、轮询计数字段 | `uq_vision_tasks_tenant_uid(tenant_id, task_uid)`、`uq_vision_tasks_tenant_dedup(tenant_id, dedup_key)`；索引 `tenant_id`、`(tenant_id, created_at DESC)`、`(tenant_id,status)`、`(tenant_id, model_name)` |
| `vision_results` | `artifact_path/bytes/sha256`、`result_sha256/schema_version`、汇总、`workflow_run_id`、`agent_run_id/event_id/trace_id`、`purged_at` | `uq_vision_results_tenant_task(tenant_id, task_id)`；索引 `tenant_id`、`(tenant_id, created_at DESC)` |
| `vision_models` | `model_key/version`、权重文件名与 SHA、类别、许可与商业提示、状态 | `uq_vision_models_tenant_key(tenant_id, model_key)`；索引 `tenant_id` |

### 5.2 复用既有表

- `vision_events`（既有事件表）承载视觉事件；`payload` 写入 `task_uid/frame_id/timestamp_ms/simulated=false` 等字段，不新增表。
- `workflow_runs` / `agent_runs` 记录联动指针；新增部分唯一索引：

```sql
CREATE UNIQUE INDEX uq_workflow_runs_vision_task
ON workflow_runs ((trigger_payload->>'vision_task_uid'))
WHERE trigger_type = 'event'
  AND trigger_payload->>'vision_task_uid' IS NOT NULL;
```

### 5.3 保留与清理

- 默认保留天数 `VISION_RESULT_RETENTION_DAYS=90`。
- 清理入口只有 `backend/scripts/vision_cleanup.py`（服务层 + CLI）：默认删除视频与 artifact，保留 PG 汇总并写审计 `vision.cleanup`；`--purge-db` 才删结果行，任务仍可查询，结果标记 `purged`。
- HTTP 未暴露清理路由（14 个 `/vision/*` 路由不含 cleanup），避免把批量删除能力挂到在线接口；如后续需要，必须另加 admin 权限、二次确认与审计。
- 越界路径只告警不删文件；删除前支持 dry-run 核对行数。

### 5.4 开发库实际状态

- 开发库已升级到 `0006_vision`（head），迁移链 `0005_obs_analytics_indexes → 0006_vision` 无分叉。
- 已写入：演示用户（seed 幂等，`unchanged=3`）、演示 Agent、`园区周界巡检（演示）`（id 1，已发布）、`视觉事件研判（演示）`（id 4，已发布）、`vision_models` 的 `yolo11n` 登记行（`model_version=yolo11n-0ebbc80d4a76`）。

## 6. 前端功能

| 页面 | 路径 | 功能 |
| --- | --- | --- |
| 识别概览 | `/vision` | 指标卡（任务/完成/失败/事件/帧/检测）、最近任务；上传表单位于 `/vision/detect` |
| 视频识别 | `/vision/detect` | 选择视频、置信度/采样参数、上传、2 秒轮询进度与错误原因 |
| 识别记录 | `/vision/records` | 状态/关键字/时间筛选、分页、跳转详情 |
| 识别详情 | `/vision/records/:taskUid` | 播放器 + 当前帧目标面板、事件列表、取消/重试/触发联动/Agent 研判、结果截断提示 |
| 模型与许可 | `/vision/models` | 模型版本/权重 SHA/设备/类别/许可双路径提示、服务实时状态 |

要点：

- **播放器坐标匹配**：`VisionPlayer` 按 `video.currentTime*1000` 与 `timestamp_ms` 二分/就近匹配帧，bbox 以视频原始宽高归一化后乘渲染尺寸，窗口缩放重绘；不传高频帧，不使用 WebRTC。
- **结果全量**：`visionApi.results` 默认请求 `limit=2000`（后端上限），避免默认 300 帧截断导致错帧；`query.total > frames.length` 时显示非阻塞提示。
- **任务隔离**：切换任务时递增 `resultsToken` 与校验 `task_uid`，在途响应不会串到新任务。
- **权限隐藏**：路由对全部登录角色可见；上传/取消/重试/触发/研判按钮仅 admin/operator 渲染，后端仍二次校验。
- **降级体验**：vision 不可达时页面显示明确原因，不展示 Mock 检测结果；浏览器不支持 H.265 时只显示明确错误，不做转码。
- 事件中心前端页面仍是既有占位实现；本模块只在 Vision 页面展示视觉事件，未改动事件中心。

## 7. 测试执行结果与通过率

执行日期 2026-11-26，Windows 11，Python 3.11，真实权重与 `vtest.avi` 已就位。

| 套件 | 命令 | 结果 | 通过率 | 耗时 |
| --- | --- | --- | --- | --- |
| 后端全量 | `cd backend && RUN_REAL_YOLO=1 VISION_REAL_EVIDENCE_JSON=… python -m pytest -q` | **701 passed**，0 failed / 0 skipped | 100% | 165.71s |
| 推理服务全量 | `cd vision-service && RUN_REAL_YOLO=1 VISION_MODEL_PATH=weights/yolo11n.pt python -m pytest -q` | **38 passed**（含真实推理） | 100% | 135.39s |
| 前端单测 | `cd web && npm test -- --run` | **131 passed / 20 files** | 100% | 70.69s |
| 前端类型 | `cd web && npx vue-tsc --noEmit` | 退出码 0，无错误 | — | — |
| **合计** | — | **870 passed / 0 failed** | **100%** | — |

说明：后端真实证据用例由 `RUN_REAL_YOLO=1` 与 `VISION_REAL_EVIDENCE_JSON` 双重门控；未设置门控时该用例 skip，其余照常执行，绝不生成假 person 检测。单独执行 `test_inference_real.py` 为 1 passed，耗时 90.81s。

### 7.1 视觉模块用例分布

- 后端视觉测试：15 个文件、220 个用例（含参数化展开；其中 `test_vision_api.py` 36、`test_vision_media.py` 24、`test_vision_client.py` 24、`test_vision_upload.py` 20、`test_vision_agent.py` 16、`test_vision_storage.py` 15、`test_vision_sync.py` 14、`test_vision_schemas.py` 14、`test_vision_trigger.py` 13、`test_vision_rules_events.py` 13、`test_vision_completion.py` 9、`test_vision_cleanup.py` 9、`test_vision_resilience.py` 6、`test_vision_acceptance.py` 5、`test_vision_linkage.py` 2）。
- 前端视觉测试：5 个文件、47 个用例（pages 19、utils 14、player 7、store 4、api 3）。
- 推理服务：8 个测试文件、38 个用例，含真实视频推理与标准化/作业/配置/API。

### 7.2 验收项覆盖

| 验收要求 | 覆盖用例 |
| --- | --- |
| 真实视频推理，至少 1 个 person，bbox 在界内，`simulated=false` | `test_inference_real.py`；`test_vision_acceptance.py::test_real_video_evidence_is_consumed_from_task6_gate`；真实事件见 `docs/evidence/vision/live-run/events.json` |
| 重复上传 + 并发上传只有一个任务 | `test_vision_resilience.py::test_duplicate_upload_returns_existing_task`、`::test_concurrent_same_file_creates_one_task`；`test_vision_upload.py` |
| 推理服务重启：未完成 `failed(service_restarted)`，已完成可查 | `test_vision_resilience.py::test_service_restarted_fails_unfinished_and_keeps_completed_queryable`、`::test_poller_resumes_task_state_from_db_after_backend_restart` |
| 跨租户任务/事件/结果/视频全部不可见 | `test_vision_acceptance.py::test_cross_tenant_task_events_results_and_video_are_hidden`；`test_vision_api.py` |
| 签名 URL：1h 内可用、过期/篡改 403、Range 206、非法 416 | `test_vision_acceptance.py::test_signed_video_url_ttl_range_and_tamper_rejected`；`test_vision_media.py`；`test_vision_api.py` |
| 事件重放不新增事件、不新增 Run | `test_vision_resilience.py::test_replayed_ingest_and_trigger_do_not_duplicate_events_or_runs`；`test_vision_linkage.py` |
| vision 停止不影响登录/Agent/工作流/事件/运行列表 | `test_vision_acceptance.py::test_vision_service_down_keeps_existing_modules_available`、`::test_vision_disabled_returns_503_only_for_vision_routes` |

### 7.3 Mock 边界

- 视觉检测**没有 Mock 路径**：推理服务不可达、权重缺失或 artifact 损坏时明确失败。
- Mock LLM 只用于 Agent/工作流研判文本（`demo_vision` fixture），**不计入视觉识别验收**；真实视频验收以第 8 节的真实检测结果为准。
- 任务详情、事件、工作流运行均记录 `simulated=false` 与真实 `model_version`。

## 8. 真实视频推理证据

| 项 | 值 |
| --- | --- |
| 素材 | OpenCV 公开样例 `vtest.avi`，<https://raw.githubusercontent.com/opencv/opencv/master/samples/data/vtest.avi> |
| 素材大小 / SHA-256 | 8,131,690 bytes / `45cddc9490be69345cbdab64ca583be65987e864ca408038e648db99e10516cf` |
| 权重 SHA-256 | `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1` |
| 推理环境 | CPU，torch 2.5.1+cpu，ultralytics 8.4.219，imgsz=640 |
| 视频规格 | 768×576，10 FPS，795 帧，79,500 ms |
| 抽帧 | `sampled_every_n_frames=1`，处理 795 帧（目标 900、上限 2000） |
| 检测 | 6,790 次；person 4,618 / truck 805 / car 790 / dog 450 / kite 66 / frisbee 33 / surfboard 25 / skateboard 2 / skis 1 |
| 最高置信度 | 0.9168 |
| 直接推理耗时 | 77,362 ms（独立脚本运行）；Compose 实跑同一视频为 46,930 ms（两次独立运行、主机负载不同） |
| artifact | `vision-result/v1`，`result.json` 2,089,093 bytes，SHA-256 `d6829b00247d105bd3d655a961b1efdf04910797dd3a4c025a49339c7fd11149` |
| 标注图 | `docs/evidence/vision/frame-138.png`，690,005 bytes，SHA-256 `d0c906d7c8459028ec70fac7584603537972ae9868bd711a2a6a565b21ba2f06` |

复现命令：

```bash
cd backend && python scripts/fetch_vision_sample.py     # 下载并打印 SHA
cd ../vision-service && RUN_REAL_YOLO=1 VISION_MODEL_PATH=weights/yolo11n.pt \
  .venv/Scripts/python.exe -m pytest tests/test_inference_real.py -v
```

### 8.1 Docker Compose 端到端实跑

同一次真实上传经 `--profile app --profile vision` 全链路：

| 事实 | 值 |
| --- | --- |
| 任务 | `0c3649fd-beda-4a22-87e6-0c608dc744cc`，completed，795 帧 / 6,790 检测 / 16 事件 / 46,930 ms |
| 模型版本 | `yolo11n-0ebbc80d4a76` |
| 重复上传 | `deduplicated=true`，复用同一 `task_uid` |
| 签名视频 | `Range: bytes=0-1023` → 206 `bytes 0-1023/8131690`；越界 → 416；篡改 token → 403 |
| 事件联动 | 工作流 4「视觉事件研判（演示）」Run #7 succeeded，携带 16 个 `vision_event_ids` |
| Agent 研判 | AgentRun #253 succeeded，trace `e8bed247f46939ef7928cf7d1652945f` |
| 任务状态 | 研判成功后仍为 completed（联动失败不影响推理状态） |

响应原文见 [`docs/evidence/vision/live-run/`](../evidence/vision/live-run/)，证据说明见 [`docs/evidence/vision/README.md`](../evidence/vision/README.md)。视频与完整 artifact 不入库，按上表 SHA 可本地复现。

## 9. Docker 启动方式

### 9.1 原有应用（不含视觉）

```bash
docker compose -f docker/compose/docker-compose.yml --profile app up -d --build
```

### 9.2 应用 + 视觉（新增 `vision` profile）

```bash
# 1) 首次准备：权重与演示视频（均不入镜像/仓库）
cd vision-service && python scripts/fetch_model.py && cd ..
python backend/scripts/fetch_vision_sample.py

# 2) 启动（vision 仅监听 127.0.0.1:8001）
docker compose -f docker/compose/docker-compose.yml \
  --profile app --profile vision up -d --build

# 3) 权重复制进运行中的容器后必须 reload
docker compose -f docker/compose/docker-compose.yml cp \
  vision-service/weights/yolo11n.pt vision:/app/weights/yolo11n.pt
curl -X POST -H "X-Vision-Token: $VISION_SERVICE_TOKEN" \
  http://127.0.0.1:8001/v1/models/reload

# 4) 登记模型元数据（幂等）
cd backend && python scripts/seed_vision_demo.py
```

- 卷：`vision_data:/data/vision`（视频、artifact、state.json），`vision_weights:/app/weights`；`down -v` 会删除两者。
- 镜像：`edgepilot-vision:local` 约 2.79 GB（CPU torch + ultralytics），`edgepilot-api:local` 约 493 MB。
- `api` 通过 `VISION_SERVICE_URL=http://vision:8001` 访问；`VISION_ENABLED=false` 时全量视觉路由 503。
- 未启动 vision 时，EdgePilot 登录、Agent、工作流、设备、事件中心不受影响；`/vision/health` 返回 `reachable=false`。
- 单独重启推理服务：`docker compose ... --profile vision restart vision`，不会重启 api。

## 10. 已实现 / 未实现 / 后续规划

### 10.1 已实现

- 视频上传（mp4/mov/m4v/webm/mkv/avi）：200MB、900 秒、魔数/扩展名校验、`422 video_too_long`。
- 指纹幂等、并发唯一约束、失败记录、任务取消/重试（最多 3 次）。
- 真实 YOLO11n CPU 推理、抽帧目标 900 / 硬上限 2000、PTS 优先时间戳、`frame_id` 保留。
- 标准化 `vision-result/v1`，artifact 原子写、SHA/摘要/schema 校验。
- 2 秒轮询、指数退避上限 30 秒、30 次失败、3 次 404、1800 秒超时、终态停止、重启恢复。
- 事件生成（`vision.detection` 等规则）、确定性 `dedup_key`、5 秒冷却、数据库唯一约束、重放不重触发。
- 工作流任务级一次触发（部分唯一索引），失败不改推理状态；Agent 任务级研判与运行复用，记录 `task_id/event_id/workflow_run_id/agent_run_id/trace_id`。
- 签名视频 URL（1h、租户/任务/文件绑定、Range 206/416、常量时间比较、URL 不写日志）。
- 14 个后端路由、8 个推理服务路由、租户隔离与角色校验。
- 5 个前端页面 + 播放器叠框 + 当前帧目标面板 + 结果全量拉取与任务隔离。
- 保留/清理（默认删文件留摘要，审计 `vision.cleanup`）、Docker `vision` profile、演示种子与素材下载脚本、9 份文档。

### 10.2 未实现（明确边界）

| 能力 | 现状 |
| --- | --- |
| 摄像头 / RTSP / WebRTC 实时流 | 不支持；一期只处理上传视频 |
| 目标跟踪（track_id） | 不做跟踪；`track_id` 字段保留为 `null` |
| 转码 / HLS / 浏览器不支持的编码 | 不做转码；H.265 等不支持时只显示明确错误 |
| 对象存储（S3 等） | 只支持本地/共享卷存储 |
| 恶意文件扫描 / 病毒检测 | 只做类型、魔数、大小、时长校验 |
| 模型训练、微调、优化、TensorRT/ONNX 导出 | 明确不做，只做预训练权重推理 |
| GPU 强依赖 | 不需要；CPU 为默认与验收环境 |
| 多 API 副本 | 当前按单实例设计；多副本需要 PostgreSQL advisory lock |
| 事件中心前端页面 | 仍为既有占位；本模块只在 Vision 页面展示事件 |
| Grafana / Prometheus / 外部监控 | 按需求不引入 |
| 区域绘制 UI | 只接受归一化多边形 JSON（`zones`），不做画布编辑 |
| 实时逐帧推送 | 不传高频帧；前端按播放时间匹配已落盘结果 |

### 10.3 后续规划（按优先级）

1. **多副本**：为任务派发与轮询加 PostgreSQL advisory lock，验证水平扩展下的幂等。
2. **对象存储**：抽象 `VisionStorage` 后端，支持 S3/OSS 与生命周期策略。
3. **流式输入**：RTSP/摄像头接入、转码与 HLS，保持与现有任务模型兼容。
4. **跟踪与轨迹**：在标准化层增加 `track_id`，用于区域停留/越界规则。
5. **区域编辑 UI**：在 `Detect.vue` 增加画布绘制，直接产出归一化 `zones`。
6. **事件中心联动页面**：把视觉事件纳入既有事件中心的可视化与确认流程。
7. **GPU 与批推理**：可选的 CUDA/TensorRT 路径，保持 CPU 默认可运行。

### 10.4 已知偏差与设计取舍

| 取舍 | 说明 | 影响 |
| --- | --- | --- |
| 视觉页面路由对全部登录角色可见 | 与后端读接口一致；写操作按钮与接口双重限 admin/operator | viewer 只读，不越权 |
| `vision` 只绑定 `127.0.0.1:8001` | 仅 api 经 compose 网络访问；避免局域网直连单 CPU worker | 局域网调试需显式改端口映射 |
| ER 文档写「Vision 新增 3 张表」 | `vision_events` 为既有表复用，只有 3 张新表 | 表数量描述准确 |
| 详情不返回逐帧 `processed_frames` | 与清理契约测试一致；汇总在结果与列表接口 | 前端类型允许可选 |
| 清理先删文件后提交 DB | 提交失败时文件已删、行指向缺失；结果 404 `artifact_missing`，重跑收敛 | 极窄窗口内需重跑清理 |
| 事件 `occurred_at` 以任务创建时间为基准加帧 `timestamp_ms` | 保留视频内相对时序；绝对时间以任务创建为锚点 | 与墙钟存在偏差，帧内偏移看 `payload.timestamp_ms` |
| 研判复用同一 AgentRun | 同任务重复点击不产生新运行 | 结果以最新一次为准 |

---

**交付物对照**：1 文件清单 → §1；2 架构与数据流 → §2；3 模型与版本 → §3；4 API 清单 → §4；5 DB 变更 → §5；6 前端功能 → §6；7 测试结果与通过率 → §7；8 真实视频推理证据 → §8 与 `docs/evidence/vision/`；9 Docker 启动 → §9；10 已实现/未实现/后续规划 → §10。
