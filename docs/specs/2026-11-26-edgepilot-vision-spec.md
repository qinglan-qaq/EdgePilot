# EdgePilot Vision Center 规格说明（Spec v1，审查版）

| 项 | 内容 |
| --- | --- |
| 文档状态 | **Draft / 待审查**。本 spec 通过前不进入编码实施。 |
| 日期 | 2026-11-26 |
| 模块 | YOLO 视觉识别（Vision Center） |
| 目标仓库 | `E:\EdgePilot`（FastAPI + SQLAlchemy async + Alembic + Vue3 / Element Plus） |
| 关联需求 | 视频输入 → 真实 YOLO 推理 → 标准化结果 → 前端展示 → 业务事件 → Agent / 工作流联动 |
| 审查方式 | 先确认第 4 节设计决策与第 20 节开放决策，再确认第 21 节阶段门禁 |

> 本文档只描述规格与契约，不包含可执行代码。文中 SQL / JSON 均为契约示例。

---

## 1. 背景与目标

EdgePilot 现有六类 Agent、工作流、事件中心与可观测性，但视觉链路只有规则引擎与 `MockDetector`，`YoloDetector` 是构造即抛 `NotImplementedError` 的占位。README 的 P8 边界明确写着“不做 YOLO 真实推理”。

本模块补上这条真实链路，目标是：

1. 用户上传视频后，由**真正的预训练 YOLO 模型**完成推理，不训练、不微调、不用 Mock 冒充。
2. 检测结果经 Adapter 转成统一 JSON，业务层不接触 Ultralytics 原始对象。
3. 前端按视频播放时间显示对应帧的目标框，框与画面坐标一致。
4. 检测结果可生成 `vision_events` 业务事件，并触发已发布工作流或直接调用 Vision Agent 研判。
5. 任务异步执行、状态持久化、可取消、可重试、可追溯；视频二进制不入 PostgreSQL。
6. 不破坏既有 Agent、工作流、设备、事件中心、可观测性功能。

## 2. 范围

### 2.1 本期实现（In scope）

- 本地视频文件上传（mp4 / mov / webm / avi / mkv / m4v），单文件上限 200MB。
- 独立推理服务 `vision-service`：YOLO11n 预训练权重、CPU 推理、异步任务、状态文件、结果 artifact。
- 后端任务编排：上传落盘、幂等、派发、轮询对账、取消、有限重试、重启恢复。
- 数据库新增 3 张表：`vision_tasks`、`vision_results`、`vision_models`。
- 后端 Vision API：任务、结果、视频流、事件、模型、健康、概览、研判。
- 前端 4 个页面：总览、视频识别、识别记录、模型管理；视频播放 + Canvas 目标框叠加。
- 事件：`vision.detection`、`security.zone_intrusion`，写入既有 `vision_events`。
- 联动：事件触发已发布工作流（`trigger_type=event`）；直接 Vision Agent 研判；Trace 关联。
- 单元、集成、前端、真实推理测试；Docker Compose 编排；文档更新。

### 2.2 本期明确不做（Out of scope，且前端/文档必须标注）

- 实时摄像头、RTSP / GB28181 拉流、WebRTC。
- 目标跟踪（`track_id` 恒为 `null`，不伪造身份连续性）。
- 模型训练、微调、自定义数据集、TensorRT / ONNX 导出。
- GPU 作为运行前提（`VISION_DEVICE=cuda` 为可选，默认 CPU）。
- 视频转码、HLS 切片、CDN、对象存储。
- 恶意文件扫描、内容审核。
- Grafana / Prometheus 等外部监控系统接入。
- 多模型并行调度与模型热切换 UI。
- 事件中心前端页面本身（当前仍是 `ModulePending`）；本模块只在其数据表中写入事件，并在 Vision 页面内展示事件摘要。

## 3. 现有系统基线（代码审查结论）

| 关注点 | 现状（真实代码） | 对本模块的约束 |
| --- | --- | --- |
| API 信封 | `ok()` → `{code, message, data, trace_id}`；`AppError` 统一异常 | 新接口必须复用 |
| 分页 | `PageParams(page>=1, size 1..200)`、`PageResult` | 列表接口复用 |
| 租户 | `TenantMixin`、`get_tenant_id()`、仓储显式过滤；跨租户 404 | 新表全部带 `tenant_id` |
| 鉴权 | `get_current_user`、`require_role("admin","operator")` | 写操作限 admin/operator |
| 迁移 | 单线，head = `0005_obs_analytics_indexes` | 新迁移 `0006_vision` |
| 视觉事件 | `vision_events`：`event_type String(32)`、`bbox JSONB`、`model_version String(32)`、`dedup_key` 唯一 `(tenant_id,dedup_key)` | 新事件直接写该表，不改结构 |
| 工作流 | `RunService.create()` 是唯一建 Run 入口；要求已发布；`trigger_payload` 可存来源 | 事件触发复用 |
| Agent | `run_agent()` 单入口；`POST /agents/{id}/test` 可试跑 | 直接研判复用 |
| 观测 | `Tracer` / `span` / trace 树 | Agent 研判与工作流 Run 有 trace；推理任务本身不建 trace |
| 前端 | 路由 `meta.nav` 生成导航；`api` 已解包信封；`AsyncState` 三态 | 新页面沿用 |
| 部署 | compose project `edgepilot`，profile `app`；`api/worker` 未挂 `var/` | 新增共享卷，保持原启动方式 |
| 模型抽象 | `Detector` 协议 + `MockDetector` + `YoloDetector` 占位 | 真实推理放独立服务；不改旧抽象冒充实现 |

## 4. 关键设计决策

| 编号 | 决策 | 理由 | 被否决的备选 |
| --- | --- | --- | --- |
| D1 | 模型 **YOLO11n**（`yolo11n.pt`），COCO 80 类，CPU 推理 | 5.6MB、CPU 可跑、无需训练；满足“轻量预训练” | YOLO11s/m：CPU 更慢；自训练：超出范围 |
| D2 | 推理放**独立 `vision-service`**（FastAPI，端口 8001） | torch/ultralytics 体积大；独立进程不污染 backend 镜像；可单测、可重启、可换 GPU | 直接在 backend 进程推理：镜像膨胀、CPU 争用、依赖冲突 |
| D3 | **后端轮询** vision-service（默认 2s） | 不引入 Celery/RabbitMQ 新队列；vision-service 自持状态文件；后端重启可恢复 | Celery 队列：改动大；WebSocket 回调：增加长连接复杂度 |
| D4 | 帧级检测放 **JSON artifact 文件**；DB 只存任务汇总与 artifact 指针 | 避免单任务数千行写入与 PG 膨胀；天然满足“视频/结果/元数据分开管理” | 每检测一行入库：写入放大、查询代价高 |
| D5 | 视频存**共享卷**，绝不入库 | 明确要求；文件大小与备份策略独立 | bytea 入库：禁用 |
| D6 | 视频播放用 **签名 URL + Range** | `<video>` 不能带 Authorization 头；签名绑定租户与任务 | 把 access token 拼进 URL：泄露面更大 |
| D7 | 事件→工作流与直接 Agent **两者都做** | 用户明确选择；工作流负责编排，直接研判用于快速验证 | 只做一种：不满足需求 |
| D8 | 自动抽帧：以 **目标 900 帧** 反推步长 | 长视频 CPU 可控；保留原始 `frame_id` 与真实时间戳 | 每帧推理：10 分钟视频不可接受 |
| D9 | 事件按**类别/区域冷却窗口**节流（默认 5s） | 避免一次视频产生上千条事件 | 每帧每目标一条：事件风暴 |
| D10 | 同文件同参数重复上传返回**既有任务** | 幂等要求；避免重复推理与重复事件 | 每次新建任务：重复计算、事件重复（虽有 dedup） |
| D11 | **不做** Mock 结果兜底 | 明确要求；失败必须显示失败原因 | 失败时返回假框：禁用 |
| D12 | 模型许可**不默认商用** | Ultralytics AGPL-3.0；需显式提示 | 默认“可商用”：不实 |

### 4.1 许可证事实

- 模型：Ultralytics YOLO11n，预训练于 COCO；权重不提交仓库。
- 许可证：**AGPL-3.0**（Ultralytics 官方）。
- 商业使用：**不默认已获授权**。闭源商用、SaaS 对外提供、分发衍生作品前，需自行评估 AGPL 义务或购买 Ultralytics Enterprise License。
- 展示位置：`/vision/models` 页面固定提示；`vision_models.commercial_use_notice`；README 与 spec 同步。

## 5. 总体架构

### 5.1 组件

```
┌──────────────┐      multipart/JSON       ┌─────────────────────────────┐
│  Browser     │ ────────────────────────▶ │ Backend API (8000)          │
│  Vue3        │ ◀──────────────────────── │  /api/v1/vision/*           │
└──────────────┘   envelope + signed URL    │  VisionTaskService          │
        │                                   │  VisionEventService         │
        │ <video> Range                     │  VisionTriggerService       │
        ▼                                   │  VisionSyncPoller (2s)      │
┌──────────────────────┐                    └──────────┬──────────────────┘
│ Shared volume        │                               │ HTTP
│  videos/<tenant>/... │                               ▼
│  jobs/<uid>/...      │                    ┌─────────────────────────────┐
└──────────────────────┘ ◀──── writes ────── │ vision-service (8001)       │
        ▲                                    │  JobManager / ThreadPool     │
        └──────── reads artifact ─────────── │  Ultralytics YOLO11n (CPU)   │
                                             └─────────────────────────────┘
                                                          │
                                                          ▼
                                             PostgreSQL: vision_tasks /
                                             vision_results / vision_models /
                                             vision_events / workflow_runs
```

### 5.2 上传到展示的时序

1. 前端校验文件类型/大小，POST multipart 到 `/api/v1/vision/tasks`。
2. 后端流式写盘到 `VISION_STORAGE_DIR/videos/<tenant_id>/<task_uid>.<ext>`，同时计算 SHA-256 与大小。
3. 后端计算 `dedup_key = sha256(video_sha256 | conf | stride | canonical(zones))`；若 `(tenant_id,dedup_key)` 已存在，删除新文件并返回既有任务（`deduplicated=true`）。并发同文件上传由唯一约束兜底：捕获 `IntegrityError` 后回查既有任务、删除新文件，不返回 500。
4. 新建 `vision_tasks(status=pending)`，计算有效步长（见 6.3），POST `vision-service /v1/jobs`。
5. vision-service 入队，状态文件写 `pending`；线程池开始后写 `running` 与进度。
6. 后端轮询任务（2s）：同步 `status/progress/model/summary/error`。
7. 任务 `completed`：后端读取 artifact，校验 SHA-256，写 `vision_results`，生成 `vision_events`，再触发工作流 / 记录联动结果。
8. 前端轮询任务详情，完成后用签名 URL 播放视频，并拉取 `/results` 做 Canvas 叠加。

### 5.3 部署拓扑

- 本机开发：backend（uvicorn 8000）+ vision-service（uvicorn 8001）两个进程，共享 `backend/var/vision`。
- Docker：`api` 与 `vision` 两个服务；`vision_data` 卷同时挂到 `/data/vision`；`vision_weights` 卷挂模型。
- `worker` / `scheduler` 不需要 vision 配置；工作流 Run 由既有 worker 执行。

## 6. 功能规格

### 6.1 上传与校验

- 接收：`multipart/form-data`，字段 `file` 必填。
- 允许后缀：`.mp4 .mov .webm .avi .mkv .m4v`。
- 容器魔数校验（至少匹配一项）：
  - MP4/MOV：偏移 4 起为 `ftyp`；
  - WebM/MKV：`1A 45 DF A3`；
  - AVI：`RIFF` + 偏移 8 为 `AVI `。
- 大小：`1 B ≤ size ≤ VISION_MAX_UPLOAD_MB`（默认 200MB）。流式写入，边写边计数，超限立即中止并删除临时文件。
- 文件名：仅保留 basename，去除控制字符；落盘名固定为 `<task_uid>.<ext>`，**绝不拼接用户文件名**。
- 时长：由 vision-service 解码后回报；若超过 `VISION_MAX_VIDEO_SECONDS`（默认 900s）仍允许完成，但抽帧步长自动放大，并在任务详情标注 `sampled_every_n_frames`。
- 磁盘失败：返回 507 `storage_write_failed`，不建任务。
- 上传成功但 vision-service 不可达：任务落库为 `failed`，`error_code=vision_service_unavailable`，响应 200（任务可查、可重试）。

### 6.2 任务生命周期

见第 10 节状态机。摘要：

- `pending`：已落库/已派发，等待推理。
- `running`：推理中，`progress 0..99`。
- `completed`：有 `vision_results`，`progress=100`。
- `failed`：有 `error_code` 与可读 `error_message`。
- `cancelled`：用户取消；不再写结果、不再生成事件。

### 6.3 推理与抽帧

- 推理参数：`imgsz=640`、`iou=0.45`、`max_det=100`、`conf=请求阈值`、`device=cpu`。
- 抽帧规则：
  - 请求可显式给 `frame_stride`（1..300）。
  - 未给时后端按目标帧数计算：`stride = max(1, ceil(estimated_frames / VISION_TARGET_FRAMES))`，其中 `estimated_frames = duration_s * fps`（后端先用 ffprobe 读取；读不到则先按 30fps 估算，服务完成后以 artifact 为准）。
  - 无论步长多少，**保留原始 `frame_id` 与真实 `timestamp_ms`**。
- 每帧记录包含空检测（`detections=[]`），用于前端按时间显示“本帧无目标”。
- 结果汇总：处理帧数、检测数、类别计数、峰值置信度、总推理耗时。

### 6.4 事件生成

输入：artifact 的 `frames` 与 `vision_tasks`。

**规则 A：基础检测事件 `vision.detection`**

- 条件：某帧存在 `class_name == "person"` 且该目标 `confidence >= VISION_EVENT_CONF`（默认 0.6）。
- 每帧最多取置信度最高的 person 生成一条候选。
- 冷却：同一任务内，`timestamp_ms` 与上次同类型事件相差 `< VISION_EVENT_COOLDOWN_S * 1000` 时丢弃。
- `severity`：`medium`（可配置）。
- `occurred_at = task.created_at + timestamp_ms`（视频时间轴，允许早于当前时间）。
- `bbox`：转换为既有事件约定的 `{x, y, w, h}`（原始像素）。
- `confidence`：该目标置信度。
- `track_id`：`null`（本期不做跟踪）。

**规则 B：区域入侵 `security.zone_intrusion`**

- 任务请求可带 `zones`：`[{"zone_id":"gate","name":"东门","polygon":[[0.12,0.2],...]}]`，坐标为**归一化 0..1**。
- 条件：person 的 bbox 中心点（归一化后）落在多边形内，且置信度达标。
- 每个 `zone_id` 独立冷却；同一帧命中多个区域时各生成一条。
- `severity`：`high`；`payload.zone_id` / `payload.zone_name`。
- 无 `zones` 时不产生该类型事件。

**幂等键**

```
dedup_key = "vision:{task_uid}:{event_type}:{frame_id}:{class_id}"[:128]
区域事件追加 ":zone:{zone_id}"
```

满足既有唯一约束 `(tenant_id, dedup_key)`；重放对账不会重复入库。

**payload 固定字段**

```json
{
  "source": "video_upload",
  "task_uid": "...",
  "task_id": 123,
  "video_filename": "gate.mp4",
  "frame_id": 120,
  "timestamp_ms": 4800,
  "class_name": "person",
  "detection_count": 3,
  "simulated": false,
  "zone_id": "gate",
  "zone_name": "东门"
}
```

- `device_id` / `node_id` 均为 `null`：视频不是物理设备，不伪造设备身份；前端与 Agent 以 `task_uid` 关联。
- `model_version`：来自任务（形如 `yolo11n-<sha12>`，≤32 字符）。

### 6.5 工作流联动

- 触发开关：`VISION_TRIGGER_ENABLED`（默认 true）。
- 工作流选择：当前租户内、`name == VISION_TRIGGER_WORKFLOW_NAME`、`status == published` 的第一条。
- 触发时机：任务完成且事件写入提交成功后。
- 幂等：`WorkflowRepo` 查询 `workflow_runs` 中 `trigger_type='event'` 且 `trigger_payload->>'vision_task_uid' = :uid`；存在则跳过。默认每任务只触发一次（`VISION_TRIGGER_ONCE_PER_TASK=true`）。
- `RunService.create()` 入参：
  - `input_data`：`{"task_uid","video_filename","event_ids","event_count","summary"}`
  - `trigger_type="event"`
  - `trigger_payload={"source":"vision","vision_task_id","vision_task_uid","vision_event_ids","first_event_id"}`
  - `user=None`（系统触发）
- 调用前设置租户 ContextVar（`set_tenant_id` / `reset_tenant_id`），避免后台轮询没有请求上下文。
- 工作流不存在/未发布：记录 warning，`vision_results.workflow_run_id` 保持 null；任务仍为 completed。
- 创建 Run 失败（如版本缺失）：记录 warning + `vision_results.trigger_error`（见 7.2），不回滚识别结果。
- 单实例假设：轮询器在一个 API 进程内串行执行；多副本部署需加 PostgreSQL advisory lock（列为后续规划，当前 compose 单 api）。

### 6.6 直接 Agent 研判

- 触发：`POST /vision/tasks/{uid}/analyze` 或 `POST /vision/events/{event_id}/analyze`。
- 选 Agent：当前租户 `agent_type='vision'` 且 `status='active'`，按 `id` 取第一条；没有则 409 `vision_agent_missing`。
- 取事件：任务最新一条 `vision_events`；或指定 event_id。
- 构造 task 并调用 `run_agent()`（与 `/agents/{id}/test` 相同的执行语义与返回字段）。
- 返回：与 `POST /agents/{id}/test` **逐字段一致**：`id`（即本次 agent_run id）、`agent_id`、`agent_version`、`status`、`output`、`error`、`trace_id`、`span_id`、`total_tokens`、`total_cost`、`retry_count`。
- 记录：`vision_results.agent_run_id` 更新为最近一次；Agent 失败也是 200 + `status="failed"`（沿用现有约定）。
- 权限：admin / operator。

### 6.7 页面规格速览

| 页面 | 核心内容 |
| --- | --- |
| 总览 `/vision` | 任务数、完成/失败数、处理帧数、检测数、事件数；最近 5 个任务；模型/服务状态 |
| 视频识别 `/vision/detect` | 上传、参数、任务进度、播放器 + 目标框、当前帧目标列表、事件列表、研判 |
| 识别记录 `/vision/records` | 任务表 + 状态/文件名筛选 + 分页 |
| 任务详情 `/vision/records/:taskUid` | 播放器、汇总、事件、联动 Run/Trace、研判、重试/取消 |
| 模型管理 `/vision/models` | 模型版本/设备/类别/许可提示、服务健康、最后检查时间 |

## 7. 数据模型

### 7.1 `vision_tasks`

```sql
CREATE TABLE vision_tasks (
  id                    BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  tenant_id             UUID NOT NULL,
  task_uid              VARCHAR(36) NOT NULL,
  dedup_key             VARCHAR(64) NOT NULL,
  status                VARCHAR(16) NOT NULL DEFAULT 'pending',
  source_type           VARCHAR(16) NOT NULL DEFAULT 'upload',
  video_filename        VARCHAR(255) NOT NULL,
  video_path            VARCHAR(512) NOT NULL,
  video_sha256          VARCHAR(64) NOT NULL,
  file_size_bytes       BIGINT NOT NULL,
  content_type          VARCHAR(64),
  conf_threshold        NUMERIC(4,3) NOT NULL DEFAULT 0.250,
  frame_stride          INTEGER NOT NULL DEFAULT 1,
  zones                 JSONB,
  progress              INTEGER NOT NULL DEFAULT 0,
  processed_frames      INTEGER,
  detection_count       INTEGER NOT NULL DEFAULT 0,
  inference_ms          INTEGER,
  video_fps             NUMERIC(7,3),
  video_duration_ms     INTEGER,
  video_width           INTEGER,
  video_height          INTEGER,
  frame_count           INTEGER,
  sampled_every_n_frames INTEGER,
  model_name            VARCHAR(64),
  model_version         VARCHAR(64),
  device                VARCHAR(16),
  error_code            VARCHAR(64),
  error_message         VARCHAR(512),
  dispatch_attempts     INTEGER NOT NULL DEFAULT 0,
  dispatched_at         TIMESTAMPTZ,
  started_at            TIMESTAMPTZ,
  finished_at           TIMESTAMPTZ,
  created_by            BIGINT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_vision_tasks_tenant_uid UNIQUE (tenant_id, task_uid),
  CONSTRAINT uq_vision_tasks_tenant_dedup UNIQUE (tenant_id, dedup_key)
);
CREATE INDEX ix_vision_tasks_tenant_created ON vision_tasks (tenant_id, created_at DESC);
CREATE INDEX ix_vision_tasks_tenant_status  ON vision_tasks (tenant_id, status);
CREATE INDEX ix_vision_tasks_tenant_model   ON vision_tasks (tenant_id, model_name);
```

约定：

- 不建外键（与仓库一致）。
- `video_path` 存相对 `VISION_STORAGE_DIR` 的路径，例如 `videos/<tenant>/<uid>.mp4`；不存绝对路径。
- `status` 只允许 5 个值；`conf_threshold` 3 位小数；`model_version` 允许 64 位以容纳更长版本串。

### 7.2 `vision_results`

```sql
CREATE TABLE vision_results (
  id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  tenant_id        UUID NOT NULL,
  task_id          BIGINT NOT NULL,
  task_uid         VARCHAR(36) NOT NULL,
  artifact_path    VARCHAR(512) NOT NULL,
  artifact_bytes   BIGINT NOT NULL,
  result_sha256    VARCHAR(64) NOT NULL,
  frame_count      INTEGER NOT NULL,
  detection_count  INTEGER NOT NULL,
  class_counts     JSONB,
  peak_confidence  NUMERIC(4,3),
  event_count      INTEGER NOT NULL DEFAULT 0,
  workflow_run_id  BIGINT,
  agent_run_id     BIGINT,
  trigger_error    VARCHAR(255),
  summary          JSONB,
  purged_at        TIMESTAMPTZ,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_vision_results_tenant_task UNIQUE (tenant_id, task_id)
);
CREATE INDEX ix_vision_results_tenant_created ON vision_results (tenant_id, created_at DESC);
```

- 每任务一行；帧级数据只存在 artifact。
- `artifact_path` 相对 `VISION_STORAGE_DIR`，如 `jobs/<uid>/result.json`。
- `result_sha256` 为 artifact 规范化 JSON 的 SHA-256；后端读取后重新计算并比对。
- `event_count` 为本次真正新建的事件数（幂等跳过的不计）。
- `trigger_error` 记录最近一次工作流触发失败原因（成功时清空）。

### 7.3 `vision_models`

```sql
CREATE TABLE vision_models (
  id                    BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  tenant_id             UUID NOT NULL,
  model_key             VARCHAR(64) NOT NULL,
  display_name          VARCHAR(128) NOT NULL,
  provider              VARCHAR(32) NOT NULL,
  model_version         VARCHAR(64) NOT NULL,
  weights_filename      VARCHAR(255) NOT NULL,
  weights_sha256        VARCHAR(64),
  task_type             VARCHAR(16) NOT NULL DEFAULT 'detection',
  classes               JSONB,
  input_size            INTEGER,
  license               VARCHAR(64) NOT NULL DEFAULT 'AGPL-3.0',
  license_url           VARCHAR(255),
  commercial_use_notice TEXT,
  status                VARCHAR(16) NOT NULL DEFAULT 'available',
  last_checked_at       TIMESTAMPTZ,
  notes                 TEXT,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_vision_models_tenant_key UNIQUE (tenant_id, model_key)
);
```

- 种子脚本为默认租户写入 `yolo11n` 一行。
- `GET /vision/models` 合并 DB 行与 vision-service 实时信息；实时不可达时 `live.status="unreachable"`，不覆盖 DB 中的许可证事实。
- `classes` 为 `[{"id":0,"name":"person"}, ...]`。

### 7.4 清理策略

- `VISION_RESULT_RETENTION_DAYS`（默认 90）。
- `scripts/vision_cleanup.py --days 90 [--dry-run]`：删除过期任务的视频文件与 `jobs/<uid>` 目录；默认保留 `vision_tasks` 汇总行，将 `vision_results.artifact_path` 置空并写入 `purged_at`。若确认“彻底删除”，加 `--purge-db` 删除 `vision_results` 行并把任务标为 `failed(purged)`（不新增状态值，避免状态机扩容）。
- 不在 HTTP 请求内做清理；由运维手动或后续定时任务执行。

## 8. 接口规格

统一约定：所有 backend 接口前缀 `/api/v1`，响应信封 `{code:0,message:"ok",data,trace_id}`；错误为 `AppError` 信封；分页 `page/size`，`size ≤ 200`。

### 8.1 vision-service（内部）

除 `/health` 外，若配置 `VISION_SERVICE_TOKEN`，必须带 `X-Vision-Token`。

#### `GET /health`

```json
{
  "status": "ok",
  "service": "edgepilot-vision",
  "version": "0.1.0",
  "schema_version": "vision-result/v1",
  "model": {
    "status": "ready",
    "model_name": "yolo11n",
    "model_version": "yolo11n-1a2b3c4d5e6f",
    "device": "cpu",
    "error": null
  },
  "queue": {"pending": 0, "running": 0, "known": 3}
}
```

- 权重缺失：`status="degraded"`，`model.status="missing_weights"`，HTTP 仍 200（供容器健康检查）。

#### `GET /v1/models`

```json
{
  "items": [{
    "provider": "ultralytics",
    "model_name": "yolo11n",
    "model_version": "yolo11n-1a2b3c4d5e6f",
    "weights_path": "/app/weights/yolo11n.pt",
    "weights_sha256": "…",
    "device": "cpu",
    "imgsz": 640,
    "task_type": "detection",
    "classes": [{"id": 0, "name": "person"}],
    "license": "AGPL-3.0",
    "license_url": "https://www.ultralytics.com/license",
    "commercial_use_notice": "…",
    "loaded": true,
    "load_status": "ready",
    "load_error": null,
    "runtime": "ultralytics 8.3.x"
  }],
  "count": 1,
  "license_notice": "…"
}
```

#### `POST /v1/jobs` → 202

请求：

```json
{
  "task_uid": "b3f1c2d4e5a6478a9b0c1d2e3f4a5b6c",
  "video_path": "/data/vision/videos/<tenant>/<uid>.mp4",
  "filename": "gate.mp4",
  "device_id": "video-upload",
  "params": {"conf_threshold": 0.25, "frame_stride": 5}
}
```

响应：任务状态（同 `GET /v1/jobs/{uid}`）。幂等：

- `pending` / `running` / `completed`：原样返回，`attempt` 不变。
- `failed` / `cancelled`：`attempt += 1`，删除旧 artifact，重新入队。
- 队列满：429 `detail="待处理任务已达上限…"`。

#### `GET /v1/jobs/{task_uid}`

```json
{
  "task_uid": "…",
  "status": "running",
  "progress": 42,
  "message": "推理中",
  "attempt": 1,
  "error_code": null,
  "error_message": null,
  "created_at": "2026-11-26T10:00:00+00:00",
  "started_at": "2026-11-26T10:00:01+00:00",
  "finished_at": null,
  "params": {"conf_threshold": 0.25, "frame_stride": 5},
  "video": {"filename": "gate.mp4", "width": 1280, "height": 720, "fps": 25.0, "frame_count": 750, "duration_ms": 30000, "sampled_every_n_frames": 5, "processed_frames": 150},
  "model": {"provider": "ultralytics", "model_name": "yolo11n", "model_version": "yolo11n-…", "device": "cpu", "imgsz": 640, "license": "AGPL-3.0"},
  "summary": {"processed_frames": 63, "detection_count": 40, "class_counts": {"person": 40}, "peak_confidence": 0.93, "inference_ms": 4200},
  "result_available": false,
  "result_bytes": null
}
```

#### `POST /v1/jobs/{uid}/cancel`

- 非终态：置取消标记；pending 且未开始则直接 `cancelled`；running 在下一次帧检查退出。
- 终态：原样返回（幂等）。

#### `GET /v1/jobs/{uid}/results`

- 完成：返回 artifact（第 9 节）。
- 未完成/失败/取消：404 `detail="任务尚无结果…"`。

### 8.2 backend 接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/vision/health` | 登录 | 聚合服务/模型状态；不可达时 `reachable=false` |
| GET | `/vision/overview` | 登录 | 统计卡片数据 |
| GET | `/vision/models` | 登录 | DB 模型行 + 实时状态 |
| POST | `/vision/tasks` | admin/operator | multipart 上传 |
| GET | `/vision/tasks` | 登录 | 分页列表 |
| GET | `/vision/tasks/{task_uid}` | 登录 | 详情（含 `video_url`） |
| GET | `/vision/tasks/{task_uid}/results` | 登录 | 帧级结果（过滤/分页） |
| POST | `/vision/tasks/{task_uid}/cancel` | admin/operator | 取消 |
| POST | `/vision/tasks/{task_uid}/retry` | admin/operator | 失败/取消后重试 |
| GET | `/vision/tasks/{task_uid}/events` | 登录 | 该任务事件 |
| GET | `/vision/tasks/{task_uid}/video?token=…` | 签名 | Range 视频流 |
| POST | `/vision/tasks/{task_uid}/analyze` | admin/operator | 直接 Agent 研判 |
| POST | `/vision/events/{event_id}/analyze` | admin/operator | 指定事件研判 |

#### `POST /vision/tasks`

`Content-Type: multipart/form-data`

| 字段 | 类型 | 必填 | 约束 |
| --- | --- | --- | --- |
| `file` | file | 是 | 类型/大小见 6.1 |
| `conf_threshold` | float | 否 | 0.01–0.99，默认 0.25 |
| `frame_stride` | int | 否 | 1–300；不传则按目标帧数计算 |
| `zones` | string(JSON) | 否 | 数组，最多 10 个区域、每区域 3–32 个点、坐标 0..1 |

响应 `data`：

```json
{
  "task": { "…": "字段见下方任务详情" },
  "deduplicated": false,
  "video_token_expires_at": "2026-11-26T11:00:00+00:00"
}
```

#### `GET /vision/tasks`

查询参数：`page`、`size`、`status`、`q`（文件名模糊）、`start`、`end`（创建时间）。

`items[]` 字段：`task_uid`、`video_filename`、`status`、`progress`、`video_duration_ms`、`processed_frames`、`detection_count`、`event_count`、`model_version`、`error_code`、`created_at`、`finished_at`。

#### `GET /vision/tasks/{task_uid}`

```json
{
  "task_uid": "…",
  "video_filename": "gate.mp4",
  "status": "completed",
  "progress": 100,
  "conf_threshold": 0.25,
  "frame_stride": 5,
  "video": {"width": 1280, "height": 720, "fps": 25.0, "duration_ms": 30000, "frame_count": 750, "sampled_every_n_frames": 5, "processed_frames": 150},
  "model": {"name": "yolo11n", "version": "yolo11n-…", "device": "cpu"},
  "summary": {"detection_count": 312, "class_counts": {"person": 300, "car": 12}, "peak_confidence": 0.97, "inference_ms": 8600},
  "events": {"total": 6, "by_type": {"vision.detection": 5, "security.zone_intrusion": 1}},
  "workflow_run": {"id": 88, "status": "succeeded", "trace_id": "…"},
  "agent_run": {"id": 901, "status": "succeeded", "trace_id": "…"},
  "video_url": "/api/v1/vision/tasks/<uid>/video?token=<signed>&expires=<epoch>",
  "video_token_expires_at": "2026-11-26T11:00:00+00:00",
  "error_code": null,
  "error_message": null,
  "created_at": "…",
  "started_at": "…",
  "finished_at": "…"
}
```

- 未完成时 `video_url` 仍返回（可预览上传原片），但前端默认不自动播放。
- 视频文件缺失：`video_url=null`，`video_missing=true`。

#### `GET /vision/tasks/{task_uid}/results`

查询参数：`start_ms`、`end_ms`、`class_name`、`min_confidence`、`limit`（默认 300，最大 2000）、`offset`。

```json
{
  "task_uid": "…",
  "video": {"width": 1280, "height": 720, "fps": 25.0, "duration_ms": 30000, "sampled_every_n_frames": 5},
  "model": {"model_name": "yolo11n", "model_version": "…"},
  "summary": {"processed_frames": 150, "detection_count": 312, "class_counts": {"person": 300}, "peak_confidence": 0.97},
  "query": {"start_ms": 0, "end_ms": 30000, "class_name": "person", "min_confidence": 0.25, "total": 120},
  "frames": [
    {"frame_id": 0, "timestamp_ms": 0, "inference_ms": 43.2, "detections": [
      {"detection_id": "0-0", "class_id": 0, "class_name": "person", "confidence": 0.9123, "bbox": {"x1": 120.5, "y1": 80.0, "x2": 210.2, "y2": 320.7}}
    ]}
  ]
}
```

- 后端从 artifact 读取并过滤；`total` 为过滤后的帧数。
- artifact 缺失或哈希不符：404 `artifact_missing` / 503 `artifact_corrupted`（不返回空结果冒充）。

#### `POST /vision/tasks/{uid}/cancel`

- 调用 vision-service 取消；服务不可达时本地置 `cancelled`，`error_message` 说明“推理服务不可达，任务已在平台侧取消”。
- 终态幂等返回 200。

#### `POST /vision/tasks/{uid}/retry`

- 仅 `failed` / `cancelled` 可重试，且 `dispatch_attempts < VISION_MAX_RETRIES`（默认 3）。
- 重置状态、清理旧结果行、重新派发；返回任务。
- 其它状态：409 `task_not_retryable`。

#### `GET /vision/tasks/{uid}/events`

- 分页；返回与 `/events` 相同的 `EventOut` 字段；仅该任务 `payload->>'task_uid'` 匹配的事件。
- 排序 `occurred_at DESC, id DESC`。

#### `POST /vision/tasks/{uid}/analyze`

- 需要任务至少 1 条事件，否则 409 `no_vision_events`。
- 返回与 `POST /agents/{id}/test` 一致的结构。

#### `GET /vision/overview`

```json
{
  "tasks": {"total": 12, "completed": 9, "running": 1, "failed": 2},
  "frames": {"processed_total": 3400},
  "detections": {"total": 8200},
  "events": {"total": 41, "unacked": 38},
  "model": {"status": "ready", "model_version": "yolo11n-…", "device": "cpu"},
  "recent_tasks": [{"task_uid": "…", "video_filename": "…", "status": "completed", "detection_count": 10, "created_at": "…"}]
}
```

#### `GET /vision/health`

```json
{
  "enabled": true,
  "reachable": true,
  "service": {"status": "ok", "version": "0.1.0"},
  "model": {"status": "ready", "model_name": "yolo11n", "model_version": "…", "device": "cpu", "license": "AGPL-3.0", "load_error": null},
  "error": null
}
```

- 不可达：`reachable=false`，`error="vision-service 不可达: …"`；HTTP 仍 200（便于前端展示，不当作 5xx）。

### 8.3 错误返回

沿用仓库既有约定：`code` 是**数字**（等于 HTTP 状态码），`message` 是可直接展示的中文说明，机器可读原因放 `data.reason`（即 `AppError.detail`）。前端 `ApiError.code` 类型保持 `number`，不引入字符串业务码。

| HTTP / code | data.reason | 场景 |
| --- | --- | --- |
| 400 | `invalid_file_type` | 后缀/魔数不合法 |
| 400 | `invalid_zones` | 区域结构/坐标非法 |
| 401 | `unauthorized` | 未登录/令牌失效 |
| 403 | `forbidden` | 角色不足；签名无效或过期 |
| 404 | `task_not_found` / `event_not_found` / `artifact_missing` | 不存在或跨租户 |
| 409 | `task_not_retryable` | 状态不允许重试 |
| 409 | `retry_limit_exceeded` | 重新派发次数达到 `VISION_MAX_RETRIES` |
| 409 | `no_vision_events` | 任务无事件可研判 |
| 409 | `vision_agent_missing` | 无可用 Vision Agent |
| 413 | `file_too_large` | 超过 `VISION_MAX_UPLOAD_MB` |
| 422 | `video_too_long` | 时长超过 `VISION_MAX_VIDEO_SECONDS`（probe 阶段直接拒绝，不落任务） |
| 422 | — | 其它 Pydantic 校验（沿用全局 handler） |
| 429 | `vision_queue_full` | 推理队列满 |
| 503 | `vision_disabled` | `VISION_ENABLED=false` |
| 503 | `artifact_corrupted` | artifact 哈希不符 |
| 507 | `storage_write_failed` | 磁盘写入失败 |

实现说明：`413` 与 `507` 需要新增 `PayloadTooLarge(AppError)`、`InsufficientStorage(AppError)` 两个子类（仅新增，不改既有类）；其余复用 `NotFound` / `Conflict` / `RateLimited` / `ServiceUnavailable`。

任务级 `error_code`（写入 `vision_tasks.error_code`，字符串，独立于 HTTP code）：

`video_missing`、`decode_failed`、`empty_video`、`inference_failed`、`timeout`、`cancelled`、`service_restarted`、`vision_job_lost`、`vision_service_unavailable`、`artifact_unreadable`、`purged`。

### 8.4 媒体签名 URL

- 后端返回：`/api/v1/vision/tasks/{uid}/video?token=<payload>.<sig>&expires=<epoch>`。
- `payload = base64url(json({"t": tenant_id, "u": task_uid, "e": epoch}))`
- `sig = base64url(HMAC-SHA256(key, payload))`，`key = HMAC-SHA256(jwt_secret, b"vision-media")`。
- 校验：签名恒定时间比较 + 未过期 + `tenant_id`/`task_uid` 匹配任务。
- TTL：1 小时；任务详情每次返回新 URL。
- 响应头：`Accept-Ranges: bytes`、`Cache-Control: private, max-age=3600`、`Content-Disposition: inline`。
- Range 请求返回 206 + `Content-Range`；不支持 Range 的客户端回退 200 全量。
- 验收：`curl -H "Range: bytes=0-1023"` 必须返回 206。

## 9. 标准化 JSON 契约（artifact）

文件位置：`VISION_STORAGE_DIR/jobs/<task_uid>/result.json`；同时可通过 `GET /v1/jobs/{uid}/results` 获取。

```json
{
  "schema_version": "vision-result/v1",
  "event_id": "b3f1c2d4e5a6478a9b0c1d2e3f4a5b6c",
  "event_type": "vision.video_detection",
  "timestamp": "2026-11-26T10:00:00+00:00",
  "device_id": "video-upload",
  "source": {
    "type": "video_file",
    "video_id": "b3f1c2d4e5a6478a9b0c1d2e3f4a5b6c",
    "filename": "gate.mp4",
    "width": 1280,
    "height": 720,
    "fps": 25.0,
    "frame_count": 750,
    "duration_ms": 30000,
    "sampled_every_n_frames": 5
  },
  "model": {
    "provider": "ultralytics",
    "model_name": "yolo11n",
    "model_version": "yolo11n-1a2b3c4d5e6f",
    "device": "cpu",
    "imgsz": 640,
    "license": "AGPL-3.0"
  },
  "video": {
    "filename": "gate.mp4",
    "width": 1280,
    "height": 720,
    "fps": 25.0,
    "frame_count": 750,
    "reported_frame_count": 750,
    "duration_ms": 30000,
    "sampled_every_n_frames": 5,
    "processed_frames": 150
  },
  "params": {"conf_threshold": 0.25, "frame_stride": 5, "iou": 0.45, "imgsz": 640},
  "frames": [
    {
      "frame_id": 0,
      "timestamp_ms": 0,
      "inference_ms": 43.2,
      "detections": [
        {
          "detection_id": "0-0",
          "class_id": 0,
          "class_name": "person",
          "confidence": 0.9123,
          "bbox": {"x1": 120.5, "y1": 80.0, "x2": 210.2, "y2": 320.7}
        }
      ]
    }
  ],
  "summary": {
    "processed_frames": 150,
    "detection_count": 312,
    "class_counts": {"person": 300, "car": 12},
    "peak_confidence": 0.97,
    "inference_ms": 8600
  },
  "error": null,
  "created_at": "2026-11-26T10:00:00+00:00",
  "finished_at": "2026-11-26T10:00:09+00:00"
}
```

字段约束：

- 坐标：**视频原始像素** `x1,y1,x2,y2`，保留 2 位小数，已裁到画面内。
- 置信度：0–1，4 位小数。
- `detections=[]` 合法；不允许用空数组掩盖推理失败（失败时任务为 `failed`，无 artifact）。
- `timestamp_ms = round(frame_id * 1000 / fps)`；`fps` 不可用时按 25fps 估算并在 `video.fps=null` 标注。
- artifact 总体积目标 ≤ 50MB；超过 2000 个采样帧时由后端限制步长。
- `model_version` 由权重文件名 + SHA-256 前 12 位构成；同时返回 `weights_sha256` 供核验。

## 10. 状态机与恢复

### 10.1 后端任务状态

```
                 ┌──────────┐
   上传/重试 ───▶│ pending  │
                 └────┬─────┘
       取消(未开始) │   │ 轮询到 running
                    ▼   ▼
              ┌───────────┐
              │ cancelled │◀──── 取消(运行中) ────┐
              └───────────┘                        │
                    ▲                              │
                    │                    ┌─────────┴──┐
                    │                    │  running   │
                    │                    └─────┬──────┘
                    │                 completed│failed
                    │                          ▼
                    │                  ┌───────────────┐
                    │                  │ completed     │
                    │                  └───────────────┘
                    │                  ┌───────────────┐
                    └──────────────────│ failed        │
                       重试(≤3 次)      └───────────────┘
```

- `completed` 为终态，不再重试。
- `cancelled` 可由用户显式重试（重新派发，attempt+1）。
- `failed` 重试次数达上限后只读。

### 10.2 轮询对账规则

后端每 `VISION_POLL_INTERVAL_S`（2s）扫描 `status in (pending, running)` 的任务：

| 远端状态 | 后端动作 |
| --- | --- |
| pending / running | 更新 `progress`、`model`、`video`、`summary`（若有）；保持当前状态 |
| completed | 读 artifact → 校验 sha → upsert `vision_results` → 生成事件 → 触发联动 → 任务 `completed` |
| failed | 任务 `failed`，复制 `error_code` / `error_message` |
| cancelled | 任务 `cancelled` |
| 404（job 不存在） | `miss_count += 1`；连续 3 次 → `failed(vision_job_lost)` |
| 网络/5xx | `fail_count += 1`；连续 `VISION_POLL_MAX_FAILURES`（30）次 → `failed(vision_service_unreachable)`；否则保持 |

- 对账以 `task_uid` 为键；同一任务处理加进程内锁，避免前端轮询与服务端轮询并发写。
- 结果写入使用 `INSERT ... ON CONFLICT (tenant_id, task_id) DO UPDATE`（或先查后写 + 唯一约束兜底），保证重放幂等。
- 事件生成先按 `dedup_key` 去重（唯一约束），重放不重复。
- 只有 `completed` 后才触发工作流；触发失败不回退任务状态。

### 10.3 重启恢复

- vision-service 启动扫描 `jobs/*/state.json`：
  - `completed`：保留状态与 artifact，继续可查。
  - `pending/running`：标记 `failed`，`error_code=service_restarted`，`error_message="推理服务重启导致任务中断，请重试"`。
- backend 重启：数据库状态是事实来源；轮询器继续对账。
  - 若任务 `pending` 但从未派发（`dispatch_attempts=0`），启动后补派发一次。
  - 若任务 `running`，远端返回 `service_restarted` → 任务 `failed`，用户可重试。
- worker 重启不影响本链路（不用 Celery 处理推理）。

### 10.4 幂等

| 场景 | 行为 |
| --- | --- |
| 同文件同参数重复上传 | 返回既有任务，`deduplicated=true`，不重跑 |
| 同 task_uid 重复 POST /jobs（运行中/完成） | 返回现有状态 |
| 同 task_uid 在失败/取消后再次 POST /jobs | 新 attempt，删除旧 artifact |
| 轮询重复收到 completed | upsert 结果 + dedup 事件，不重复 |
| 同事件重复触发工作流 | 按 `trigger_payload->>'vision_task_uid'` 查重，跳过 |
| 重复点击研判 | 每次产生新的 agent_run（现有语义），前端按钮防抖 |

## 11. 前端规格

### 11.1 路由与导航

| 路径 | 组件 | meta.nav | 说明 |
| --- | --- | --- | --- |
| `/vision` | `VisionLayout` | 是（group，icon=vision） | tabs 容器；路由 name `vision` |
| `/vision`（子 `''`） | `Overview` | 是 | 总览；`activeMenu=/vision` |
| `/vision/detect` | `Detect` | 是 | 视频识别 |
| `/vision/records` | `Records` | 是 | 识别记录 |
| `/vision/records/:taskUid` | `TaskDetail` | 否 | 详情；`activeMenu=/vision/records` |
| `/vision/models` | `Models` | 是 | 模型管理 |

- 与 `observability` 分组保持一致：分组路由 `meta.nav=true` + 子路由 `meta.nav=true` 才会在侧边栏生成二级菜单；详情页 `nav=false` + `activeMenu` 保持高亮。
- 所有角色可读；上传/取消/重试/研判按钮按 `auth.role` 控制（viewer 隐藏）。
- 导航图标在 `AppLayout.iconMap` 增加 `vision`（建议复用 `Camera` 或 `View` 图标组件）。

### 11.2 视频识别页

1. **上传区**：`el-upload` 拖拽；客户端校验后缀与 200MB；显示文件名/大小。
2. **参数区**：
   - 置信度阈值滑杆 0.05–0.95（默认 0.25），提示“影响推理时保留的最低置信度”。
   - 抽帧步长：`自动` / 1 / 5 / 10 / 25；默认自动，提示预计处理帧数。
   - 区域（可选）：本期**不做多边形绘制 UI**；提供“高级：区域 JSON”折叠输入框，校验通过才允许提交（开放决策 O6）。
3. **提交**：调 `POST /vision/tasks`；返回 `deduplicated=true` 时提示“已存在相同任务，已为你打开”。
4. **任务状态**：pending/running 显示进度条与“排队中/推理中”；每 2s 轮询任务详情；失败显示 `error_message` 与「重试」。
5. **播放器**：
   - `<video controls preload="metadata" :src="videoUrl">`。
   - 覆盖 `<canvas>`（`pointer-events:none`），尺寸跟随 video 显示区域。
   - `requestAnimationFrame` 播放时刷新；`timeupdate`、`seeked`、`loadedmetadata` 也触发重绘。
   - 按 `currentTime*1000` 在 `frames[].timestamp_ms` 上二分查找最近帧；直到下一帧前保持当前结果。
   - 缩放：`scaleX = displayWidth / videoWidth`，`scaleY = displayHeight / videoHeight`；矩形使用 `x1*scaleX` 等。
   - 标签：`class_name + confidence`，背景半透明；选中目标加粗。
   - 视频加载失败（编码不支持等）显示明确错误，不显示假框。
6. **检测面板**：当前帧编号/时间/推理耗时；按置信度排序；点击 → 暂停并 seek 到该帧；阈值滑杆只过滤显示，旁注“仅前端过滤，不重新推理”。
7. **汇总**：处理帧数、检测数、类别分布（CSS 条形图，不引入图表库）、峰值置信度、推理耗时。
8. **事件**：列表展示类型/级别/置信度/时间/区域；点击展开 payload；事件中心页面未实现时只在本页展示，不伪造跳转。
9. **联动**：显示已触发工作流 Run 链接（`/runs/:id`）与 Trace 链接；未触发时显示“未配置事件研判工作流”。
10. **研判**：按钮调 `POST .../analyze`；展示 Agent 状态、摘要、证据、Trace 链接；失败展示 `error`。

### 11.3 识别记录页

- 表格列：文件名、状态（tag）、进度、时长、处理帧/检测数、事件数、模型版本、创建时间。
- 筛选：状态下拉、文件名搜索（防抖 300ms）、刷新。
- 分页：20/页；空态区分“无记录”与“筛选无结果”。
- 行点击进入详情；详情页也可取消/重试。

### 11.4 模型管理页

- 顶部 `el-alert`（warning）：AGPL-3.0 与商业使用提示，常驻不可关闭。
- 模型表：名称、版本、设备、类别数、输入尺寸、许可、状态、最后检查。
- 服务状态卡片：`reachable`、模型 `load_status`、错误信息、队列。
- 不可达时显示“推理服务未启动”及启动命令提示，不展示伪造的 ready。

### 11.5 总览页

- 4 个统计卡：任务总数/完成、处理帧数、检测数、事件数。
- 最近 5 个任务表（点击进详情）。
- 模型/服务状态小卡（复用 health）。
- 空态：引导“上传第一个视频”。

### 11.6 状态与错误

- 所有数据加载用 `AsyncState`（loading/error/empty）。
- 错误文案来自 `ApiError.message`；不暴露 axios 细节。
- 请求竞态用递增 token 丢弃过期响应（沿用现有模式）。
- 轮询在页面卸载/任务终态时停止；避免后台泄漏。

### 11.7 前端测试

- `utils/vision.ts` 纯函数：最近帧二分、bbox 缩放、筛选、时间格式化。
- 组件测试：记录页渲染/空态/错误态；上传参数校验；播放器在给定 frames 下绘制调用（mock canvas）。
- `vue-tsc --noEmit` 必须通过。

## 12. 配置项

### 12.1 backend（`Settings` + `.env.example`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VISION_ENABLED` | `true` | 关闭后 vision 路由 503、轮询器不启动 |
| `VISION_SERVICE_URL` | `http://127.0.0.1:8001` | 推理服务地址 |
| `VISION_SERVICE_TOKEN` | 空 | 与 vision-service 共享；空表示不鉴权 |
| `VISION_SERVICE_TIMEOUT_S` | `5` | 派发/查询超时 |
| `VISION_POLL_INTERVAL_S` | `2` | 轮询间隔 |
| `VISION_POLL_MAX_FAILURES` | `30` | 连续失败后置 failed |
| `VISION_STORAGE_DIR` | `<storage_dir>/vision` | 视频与 artifact 根目录（相对路径按 backend cwd） |
| `VISION_MAX_UPLOAD_MB` | `200` | 上传上限 |
| `VISION_MAX_VIDEO_SECONDS` | `900` | 超过仍处理，但放大步长并提示 |
| `VISION_TARGET_FRAMES` | `900` | 自动步长目标 |
| `VISION_DEFAULT_CONF` | `0.25` | 默认阈值 |
| `VISION_MAX_FRAME_STRIDE` | `300` | 最大步长 |
| `VISION_EVENT_CONF` | `0.6` | 生成事件的最低置信度 |
| `VISION_EVENT_COOLDOWN_S` | `5` | 事件冷却 |
| `VISION_TRIGGER_ENABLED` | `true` | 事件触发工作流 |
| `VISION_TRIGGER_WORKFLOW_NAME` | `视觉事件研判（演示）` | 目标工作流名 |
| `VISION_TRIGGER_ONCE_PER_TASK` | `true` | 每任务只触发一次 |
| `VISION_AGENT_ENABLED` | `true` | 允许直接研判 |
| `VISION_MAX_RETRIES` | `3` | 手动重试上限 |
| `VISION_RESULT_RETENTION_DAYS` | `90` | 清理阈值 |

### 12.2 vision-service

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VISION_HOST` / `VISION_PORT` | `0.0.0.0` / `8001` | 监听 |
| `VISION_MODEL_PATH` | `weights/yolo11n.pt` | 权重 |
| `VISION_DEVICE` | `cpu` | `cpu` / `cuda` / `auto` |
| `VISION_IMGSZ` / `VISION_IOU` / `VISION_MAX_DET` | `640` / `0.45` / `100` | 推理参数 |
| `VISION_DEFAULT_CONF` | `0.25` | 默认阈值 |
| `VISION_JOBS_DIR` | `var/jobs` | 状态 + artifact |
| `VISION_MEDIA_ROOTS` | 空 | 允许读取的视频根目录（分隔符 `;`/`:`）；空不限制 |
| `VISION_SERVICE_TOKEN` | 空 | 内部令牌 |
| `VISION_PRELOAD` | `true` | 启动预加载 |
| `VISION_MAX_WORKERS` / `VISION_MAX_PENDING` | `1` / `32` | 并发/排队 |
| `VISION_JOB_TIMEOUT_S` | `1800` | 单任务超时 |

## 13. 安全与权限

- **角色**：读接口所有登录角色；上传/取消/重试/研判仅 admin、operator；viewer 上传按钮隐藏且接口 403。
- **租户**：所有查询显式带 `tenant_id`；跨租户统一 404。签名 token 绑定租户与任务。
- **路径**：服务器生成 `task_uid` 作为文件名；`video_path` 存相对路径；读取前 `resolve()` 并校验在 `VISION_STORAGE_DIR` 内，阻止 `..`。
- **上传**：后缀 + 魔数 + 大小；不信任 `Content-Type`；`UploadFile` 分块写盘。
- **视频访问**：签名 URL 限时 1h；不返回绝对文件路径；日志不打印 token。
- **内部服务**：`X-Vision-Token` 恒定时间比较；服务默认只监听内网/compose 网络；本机开发可关闭 token。
- **注入**：`zones` JSON 只做结构校验，不参与 SQL；文件名不拼接命令。
- **资源**：每任务最多 2000 采样帧；队列上限；单任务超时；防止 CPU 被无限占用。
- **日志脱敏**：不记录视频内容、令牌、完整文件路径（只记录 task_uid 与文件名）。
- **许可**：模型页与 README 固定提示 AGPL-3.0 与商业使用限制。

## 14. 日志与可观测性

明确**不接入 Grafana / Prometheus**。

- backend 使用现有 `logging` / 请求 ID：记录上传、幂等命中、派发、对账状态变化、事件条数、触发结果、失败原因。
- vision-service 记录任务接收、开始、进度里程碑（每 25%）、完成、失败、取消、模型加载。
- 关键日志字段：`task_uid`、`tenant_id`、`status`、`processed_frames`、`detection_count`、`event_count`、`elapsed_ms`、`error_code`。
- Trace：推理任务本身不创建 trace（不是 Agent 调用）；直接研判与工作流 Run 沿用现有 `Tracer`，前端可从任务详情跳转 Trace。
- `GET /vision/health` 与 `/vision/overview` 提供运行状态；不使用外部监控系统。

## 15. 性能、容量与清理

| 项 | 目标/限制 |
| --- | --- |
| 推理速度（参考） | YOLO11n CPU 640：约 50–150ms/帧（依赖机器） |
| 采样帧上限 | 900（自动步长目标）；硬上限 2000 |
| 15 分钟 25fps 视频 | 约 22500 帧 → 步长约 25 → 900 帧 → 约 1–2 分钟推理 |
| 上传大小 | ≤200MB |
| artifact 大小 | 典型 0.5–3MB；上限 50MB |
| 并发 | 默认 1 个推理线程；排队 ≤32 |
| 数据库 | 每任务 1 行 + 1 结果行 + 冷却后的事件行 |
| 清理 | 90 天；脚本执行，非请求内 |
| 浏览器播放 | 建议 H.264 MP4；不支持编码时明确报错（不做转码） |

## 16. Docker 与部署

### 16.1 新增镜像

`docker/vision.Dockerfile`：

- `python:3.11-slim`
- 安装 CPU 版 torch/torchvision（`--index-url https://download.pytorch.org/whl/cpu`）
- 安装 `vision-service/requirements.txt`
- `VISION_MODEL_PATH=/app/weights/yolo11n.pt`
- 启动：`uvicorn app.main:app --host 0.0.0.0 --port 8001`
- healthcheck：`python -c "urllib.request.urlopen('http://localhost:8001/health')"`

### 16.2 compose 变更

```yaml
  vision:
    image: edgepilot-vision:local
    build:
      context: ../..
      dockerfile: docker/vision.Dockerfile
    profiles: ["vision"]
    environment:
      VISION_JOBS_DIR: /data/vision/jobs
      VISION_MEDIA_ROOTS: /data/vision/videos
      VISION_MODEL_PATH: /app/weights/yolo11n.pt
      VISION_DEVICE: cpu
      VISION_SERVICE_TOKEN: ${VISION_SERVICE_TOKEN:-}
    volumes:
      - vision_data:/data/vision
      - vision_weights:/app/weights
    ports:
      - "8001:8001"        # 可选：仅调试需要
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8001/health')"]
      interval: 15s
      timeout: 5s
      retries: 20
      start_period: 30s
```

api 追加：

```yaml
    environment:
      VISION_ENABLED: "true"
      VISION_SERVICE_URL: http://vision:8001
      VISION_SERVICE_TOKEN: ${VISION_SERVICE_TOKEN:-}
      VISION_STORAGE_DIR: /data/vision
    volumes:
      - vision_data:/data/vision
```

> **不添加 `depends_on: vision`**：vision 属于可选能力，若强制依赖会导致推理服务异常时既有 API 无法启动，违反“不影响既有功能”。

volumes：

```yaml
  vision_data:
  vision_weights:
```

- worker / scheduler 不需要 vision 配置；保持原样。
- 既有启动命令 `docker compose -f docker/compose/docker-compose.yml --profile app up -d` **行为不变**（不启动 vision）。
- 启用视觉模块：`docker compose -f docker/compose/docker-compose.yml --profile app --profile vision up -d`。
- vision 未启动时，API 照常工作；`/vision/health` 返回 `reachable=false`，上传落库为 `failed(vision_service_unavailable)`，可重试。
- 首次启动权重目录为空：vision-service 报 `missing_weights`（health 200 + degraded）；运行 `docker compose exec vision python scripts/fetch_model.py` 或提前挂载权重。
- 离线安装：在有网机器执行 fetch_model，复制 `vision_weights`；或构建时 `COPY` 本地权重（不提交 git）。

### 16.3 本机开发

```bash
# 终端 1：推理服务
cd vision-service
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
python scripts/fetch_model.py
set VISION_JOBS_DIR=..\backend\var\vision\jobs
set VISION_MEDIA_ROOTS=..\backend\var\vision\videos
.venv/Scripts/python -m uvicorn app.main:app --port 8001

# 终端 2：后端
cd backend
set VISION_STORAGE_DIR=./var/vision
uvicorn app.main:app --reload --port 8000
```

## 17. 测试与验收

### 17.1 测试矩阵

| 层级 | 用例 | 是否依赖真实模型 |
| --- | --- | --- |
| vision-service 单元 | 标准化字段/坐标裁剪/空帧/汇总；参数校验；状态机；取消；重启恢复 | 否（假引擎） |
| vision-service 契约 | health/models/jobs/results/token/404/429 | 否 |
| vision-service 真实推理 | 真实 yolo11n.pt + 真实 MP4，断言 frames、person、bbox 在界内、schema 完整 | 是（`RUN_REAL_YOLO=1`） |
| backend 单元 | artifact→事件（阈值/冷却/区域/dedup）、媒体签名、zones 校验、错误映射 | 否 |
| backend 集成 | 上传/幂等/派发/轮询/完成/取消/重试/权限/跨租户/重启 | 否（假 vision 客户端） |
| 联动集成 | 事件→Run（`trigger_type=event`、payload、幂等）、直接 Agent（`agent_runs` + trace） | 否（mock LLM） |
| 前端单元 | 最近帧二分、bbox 缩放、阈值过滤、时间格式化 | 否 |
| 前端组件 | 记录页/详情空态、上传校验、错误态、权限隐藏 | 否 |
| 回归 | 全量 backend pytest + web vitest + vue-tsc | 否 |

### 17.2 验收标准

1. 真实视频上传后，任务从 pending/running 到 completed，`progress` 单调到 100。
2. `GET /results` 返回的帧时间戳与视频时长一致；前端播放到某一时刻显示最近采样帧的框。
3. 真实推理测试至少检测到 1 个目标，bbox 不越界；结果标记 `simulated=false`。
4. 同一文件同参数重复上传返回同一任务（`deduplicated=true`），不重复生成事件。
5. 取消后任务为 `cancelled`，不写结果、不生成事件。
6. 重启 vision-service 后，未完成任务变 `failed(service_restarted)`，已完成任务仍可查。
7. 事件按冷却窗口生成；`vision_events.dedup_key` 无重复；跨租户 404。
8. 事件触发已发布工作流：Run 的 `trigger_type=event`、`trigger_payload.vision_task_uid` 正确；重复轮询不重复建 Run。
9. 直接研判产生 `agent_runs` 行与 `trace_id`，前端可跳转 Trace。
10. 既有 backend 全量测试与 web 测试通过；`vue-tsc --noEmit` 通过。
11. `DELETE`/关闭 vision-service 时，其它模块（Agent、工作流、事件、设备）不受影响。
12. 视频访问必须带有效签名；过期/篡改返回 403。

### 17.3 真实视频素材

- 演示/测试素材不提交仓库。
- 首选 OpenCV 官方样例 `vtest.avi`（行人走动，便于验证 person）；脚本在运行时下载到 `vision-service/var/testdata/`，记录来源 URL 与 SHA-256。
- 离线环境通过 `VISION_TEST_VIDEO` 指定本地文件；无素材时真实测试明确 skip，不伪造通过。
- 备选：从单张已授权图片用 ffmpeg 生成短视频（仅用于演示，不声称是真实监控视频）。

## 18. 交付物映射（对应最初 10 项要求）

| # | 交付物 | 本 spec 对应章节 |
| --- | --- | --- |
| 1 | 新增/修改文件清单 | 附录 A |
| 2 | 架构与数据流 | 5、6 |
| 3 | 模型与版本信息 | 4.1、9、12 |
| 4 | 新增 API 清单 | 8 |
| 5 | DB 变更说明 | 7 |
| 6 | 前端功能说明 | 11 |
| 7 | 测试执行结果与通过率 | 17（实施后回填实测数据） |
| 8 | 真实视频推理截图/演示记录 | 17.3（实施后回填） |
| 9 | Docker 启动方式 | 16 |
| 10 | 已实现/未实现/规划 | 2、19、附录 A |

## 19. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| torch/ultralytics 镜像大、构建慢 | 部署成本 | CPU wheel 源、分层缓存、权重独立卷 |
| AGPL 许可被误认为可商用 | 合规风险 | 模型页/README/spec 固定提示；不默认授权 |
| CPU 推理慢 | 长视频等待 | 自动抽帧目标 900 帧、队列、进度可见 |
| 视频编码浏览器不支持（H.265） | 播放失败 | 明确错误提示；建议 H.264；不做转码（范围外） |
| 事件洪水 | 事件中心/工作流被刷 | 冷却窗口 + 每任务只触发一次工作流 |
| 多 API 副本重复触发 | 重复 Run | 当前单实例假设；后续 advisory lock（规划） |
| 共享卷路径不一致 | 任务读不到视频/artifact | 统一 `VISION_STORAGE_DIR=/data/vision`；启动自检日志 |
| 服务重启中断任务 | 任务失败 | 状态文件标记 `service_restarted`；有限重试 |
| 视频文件被删/损坏 | 无法回溯 | 任务详情显示 `video_missing`；artifact 哈希校验 |
| 上传大文件占磁盘 | 磁盘耗尽 | 200MB 上限、清理脚本、失败即删临时文件 |
| 结果 artifact 被篡改 | 业务误判 | `result_sha256` 校验；不通过则 `artifact_corrupted` |
| 轮询阻塞请求线程 | API 延迟 | 轮询在独立后台任务，HTTP 请求不等待推理 |

## 20. 开放决策（请审查确认）

| 编号 | 决策点 | 建议方案 | 备选 |
| --- | --- | --- | --- |
| O1 | 帧级结果存哪里 | **JSON artifact 文件 + `vision_results` 一行汇总** | 每检测一行入库（更易 SQL 统计，但写入放大） |
| O2 | 自动抽帧目标 | **900 帧**（约 1–2 分钟 CPU） | 300 帧（更快）/ 全帧（最准） |
| O3 | 事件冷却 | **5s / 类别，区域独立** | 1s（更多事件）/ 30s（更少） |
| O4 | 工作流触发频率 | **每任务一次** | 每事件一次（可能大量 Run） |
| O5 | 上传/时长上限 | **200MB / 900s** | 500MB / 1800s |
| O6 | 前端区域绘制 | **本期不做绘制 UI，仅高级 JSON 输入；区域事件可展示** | 首帧 Canvas 手绘多边形（工作量+交互复杂度高） |
| O7 | 视频访问 | **HMAC 签名 URL（1h）** | access token 放 query（泄露面更大） |
| O8 | 直接研判权限 | **admin + operator**（操作员在巡检时可直接研判） | 与既有 `/agents/{id}/test` 一致改为 **仅 admin**（现状是 admin-only） |
| O9 | 清理语义 | **默认删文件、保留任务汇总；`--purge-db` 彻底删** | 到期直接删除全部行 |
| O10 | 演示工作流 | **新建 3 节点「视觉事件研判（演示）」**，`VISION_TRIGGER_WORKFLOW_NAME` 可改指向现有工作流 | 直接复用「园区周界巡检（演示）」（含审批，链路更长） |
| O11 | 事件 `device_id` | **null + payload 标注 video_upload** | 伪造一个设备 ID（不实，反对） |
| O12 | 真实测试视频 | **运行时下载 OpenCV vtest.avi，不提交** | 提交二进制（仓库膨胀/许可不明） |
| O13 | Vision Agent 选择 | **租户内第一个 active 的 `agent_type=vision`** | 新增专用配置项指定 agent_id |
| O14 | 服务不可达时上传 | **建任务并置 failed，返回 200 供重试** | 直接 503 且不落库 |

## 21. 实施阶段与门禁

| 阶段 | 内容 | 退出条件 |
| --- | --- | --- |
| P1 审查与 spec | 本文档 | **用户确认第 20 节** |
| P2 vision-service | 服务、模型、任务、标准化、测试 | 契约测试通过；真实推理测试产出结果 |
| P3 后端集成 | 表/迁移/API/轮询/事件 | 集成测试通过；幂等/取消/重启用例通过 |
| P4 前端 | 4 页面 + 播放器 + Canvas | vitest + vue-tsc 通过；手工验收 |
| P5 联动 | 工作流 + Agent + Trace | 事件→Run、研判→agent_run 用例通过 |
| P6 交付 | Docker、文档、实测报告 | 全量回归通过；10 项交付物齐全 |

每个阶段完成后先跑测试，再进入下一阶段；任何阶段失败不跳阶。

## 附录 A：新增 / 修改文件清单

### A.1 新增

- `vision-service/`：`requirements.txt`、`pyproject.toml`、`README.md`、`app/{__init__,config,schemas,standardize,model,inference,jobs,auth,main}.py`、`scripts/fetch_model.py`、`tests/{test_standardize,test_api,test_inference_real}.py`
- `backend/app/models/vision.py`
- `backend/app/schemas/vision.py`
- `backend/app/repositories/vision_repo.py`
- `backend/app/services/{vision_client,vision_task_service,vision_event_service,vision_trigger_service,vision_sync,vision_media}.py`
- `backend/app/api/v1/vision.py`
- `backend/migrations/versions/0006_vision.py`
- `backend/tests/unit/{test_vision_standardize,test_vision_rules_events,test_vision_media_token}.py`
- `backend/tests/integration/{test_vision_api,test_vision_trigger}.py`
- `backend/scripts/{seed_vision_demo,fetch_vision_sample,vision_cleanup}.py`
- `web/src/api/vision.ts`
- `web/src/stores/vision.ts`
- `web/src/utils/vision.ts`
- `web/src/components/vision/{VisionPlayer,DetectionPanel}.vue`
- `web/src/views/vision/{VisionLayout,Overview,Detect,Records,TaskDetail,Models}.vue`
- `web/tests/{test_vision_utils,test_vision_pages}.spec.ts`
- `docker/vision.Dockerfile`

### A.2 修改

- `backend/app/models/__init__.py`（注册新模型）
- `backend/app/api/v1/router.py`（挂载 vision router）
- `backend/app/core/config.py`（vision 配置）
- `backend/app/main.py`（启停轮询器）
- `backend/.env.example`（新增 `VISION_*` 变量说明；`python-multipart` 已在 requirements 中，无需新增）
- `backend/tests/integration/test_schema_manifest.py`（3 张新表）
- `web/src/router/index.ts`
- `web/src/layouts/AppLayout.vue`
- `docker/compose/docker-compose.yml`
- `README.md`、`docs/architecture.md`、`docs/api.md`、`docs/er.md`、`docs/runbook.md`、`docs/offline-install.md`、`docs/demo-script.md`、`docs/user-flows.md`

## 附录 B：兼容性检查表

| 检查项 | 结论 |
| --- | --- |
| 是否修改既有表结构 | 否，仅新增 3 张表 |
| 是否修改既有 API 响应 | 否，仅新增路由 |
| 是否修改既有前端导航项 | 仅新增 vision 项；既有项不变 |
| 是否要求新中间件 | 否（不用 Celery/RabbitMQ 处理推理；工作流仍用既有队列） |
| 是否新增工作流节点类型 | 否（演示工作流复用既有 `query_vision_events` 工具节点） |
| 是否新增 Agent 类型 | 否（复用已注册的 `vision` 类型） |
| 是否引入外部监控 | 否 |
| 是否默认安装 GPU 依赖 | 否，CPU 版 torch |
| 是否提交模型权重 | 否 |
| 是否把视频存 PG | 否 |
| 是否使用 Mock 冒充真实结果 | 否；失败即失败 |
| 关闭 vision 后其它模块是否可用 | 是 |

---

**审查请求**：请重点确认第 4 节 D1–D12 与第 20 节 O1–O14；确认后我按第 21 节从 P2 开始实施，并在每个阶段结束后提交测试结果。 
