# EdgePilot Vision Center 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 EdgePilot 中从零实现真实 YOLO11n 视频识别链路：上传 → 独立推理服务 → 标准化 artifact → 后端任务/事件/联动 → 前端播放器叠加目标框。

**Architecture:** 新增独立 `vision-service`（FastAPI + Ultralytics YOLO11n CPU，端口 8001）负责推理与 artifact 托管；backend 负责上传落盘、任务指纹幂等、异步轮询对账、事件生成、工作流/Agent 联动、签名视频访问；前端新增 `/vision` 分组。视频与结果文件走共享卷，PostgreSQL 只存任务汇总、结果指针与事件。

**Tech Stack:** FastAPI 0.136 / Pydantic 2 / SQLAlchemy 2 async / Alembic / PostgreSQL 16 / httpx / Vue 3.5 / Element Plus / Vitest / pytest / Ultralytics YOLO11n / OpenCV。

**Spec:** `docs/specs/2026-11-26-edgepilot-vision-spec.md`

**代码基线说明:** 本计划编写前，仓库内未跟踪的 `vision-service/` 实现已按要求撤回，备份在 `E:\vision-service-wip-backup-20261126-190147`，不参与构建。P2 从空目录按本计划重建。

---

## Global Constraints

以下约束逐条来自 spec 与用户补充工程要求；所有任务隐含遵守。

- 预训练 **YOLO11n**（`yolo11n.pt`），COCO 80 类，CPU 推理；不训练、不微调、不导出。
- 模型许可 **AGPL-3.0**；**不默认授予商业使用权限**；必须同时说明“遵守 AGPL”与“购买 Ultralytics Enterprise License”两条路径。
- 禁止 Mock/伪造识别结果；失败必须有 `error_code` 与 `error_message`；空结果不得伪装成成功。
- 视频二进制**不得**写入 PostgreSQL；视频、artifact、元数据分离。
- 不接入 Grafana / Prometheus 等外部监控；复用现有 `logging`、`Tracer`、`/metrics`。
- 不修改既有 Agent 类型、工作流节点类型、设备/事件中心表结构；只新增表、路由、页面。
- 现有 `POST /agents/{id}/test` 保持 **仅 admin**；Vision 直接研判允许 **admin + operator**。
- 所有新查询显式 `tenant_id` 过滤；跨租户统一 404；写操作 admin/operator。
- 上传上限 **200MB**；视频时长上限 **900s**，超限返回明确错误码；文件大小与时长分别校验。
- 自动抽帧目标 **900 帧**；单任务硬上限 **2000 帧**；保留原始 `frame_id`。
- `timestamp_ms` 优先使用解码器 PTS（`cv2.CAP_PROP_POS_MSEC`）；PTS 不可用时回退 FPS 并在 artifact 标注。
- 轮询正常间隔 **2s**；网络异常指数退避，最大间隔 **30s**；终态立即停止；单任务最大执行 **1800s**。
- vision-service 任务状态必须持久化到 `state.json`；重启后 `pending/running` 变 `failed(service_restarted)`。
- 每个任务默认只触发 **1 次工作流**、只做 **1 次 Agent 研判**（失败允许受控重试）。
- 事件 `dedup_key` 确定性生成；同任务同类型 **5s 冷却**；DB 唯一约束兜底；重放不重复触发。
- 上传指纹包含 `tenant_id`、文件 SHA-256、模型标识、置信度阈值、目标抽帧数/步长、区域 canonical JSON。
- 媒体 URL 使用 HMAC-SHA256 签名，绑定租户 + 任务，TTL **1 小时**，常量时间比较；支持 Range/206/416。
- `result.json` 临时文件写入 + `fsync` + 原子替换；任务只有结果持久化成功后才能 `COMPLETED`。
- Docker 保持现有启动方式；vision 使用独立 `profile: ["vision"]`；**不添加 `depends_on: vision`**；vision 停止不影响既有功能。
- 所有新增能力必须有测试证据；Mock 测试不得当作真实模型验收。

### 与旧 spec 冲突时以本节为准

| 主题 | 旧 spec | 本计划执行 |
| --- | --- | --- |
| 超过 900s | 允许完成并放大步长 | **拒绝**：`422 video_too_long`；不进推理 |
| 抽帧上限 | 只提 900 目标 | **900 目标 + 2000 硬上限** |
| 时间戳 | 按 FPS 推算 | **PTS 优先**，FPS 回退并标注 |
| Agent 研判 | 每次点击都新建 run | **每任务默认一次**；失败可重试 |
| 媒体签名 | payload + 独立 expires | **payload 内含 exp + 独立 expires 双重校验** |
| 结果表字段 | 无 schema_version/触发状态 | 新增 `result_schema_version`、`trigger_status`、`agent_status` 等 |

---

## 文件结构与职责

```text
vision-service/
  app/config.py          # 环境变量、限额、许可常量
  app/schemas.py         # 内部 HTTP 契约
  app/auth.py            # 共享令牌校验（常量时间）
  app/model.py           # 权重登记、懒加载、状态机
  app/standardize.py     # Ultralytics -> 统一 JSON Adapter
  app/inference.py       # probe / 解帧 / PTS / 抽帧 / 推理
  app/jobs.py            # 任务状态持久化、原子写、恢复、取消
  app/main.py            # FastAPI 路由与异常映射
  tests/                 # 契约、边界、真实推理（门控）

backend/app/models/vision.py          # 3 张新表
backend/app/schemas/vision.py         # 校验 + 出参
backend/app/repositories/vision_repo.py
backend/app/services/vision_client.py # 内部 HTTP 客户端
backend/app/services/vision_storage.py# 落盘、路径、artifact 校验
backend/app/services/vision_task_service.py
backend/app/services/vision_result_service.py
backend/app/services/vision_event_service.py
backend/app/services/vision_trigger_service.py
backend/app/services/vision_agent_service.py
backend/app/services/vision_cleanup_service.py
backend/app/services/vision_sync.py   # 轮询、退避、超时、恢复
backend/app/services/vision_media.py  # HMAC + Range
backend/app/api/v1/vision.py
backend/migrations/versions/0006_vision.py

web/src/api/vision.ts
web/src/stores/vision.ts
web/src/utils/vision.ts
web/src/components/vision/VisionPlayer.vue
web/src/components/vision/DetectionPanel.vue
web/src/views/vision/{VisionLayout,Overview,Detect,Records,TaskDetail,Models}.vue
```

---

## Phase P0：撤回与准备（已完成）

- [x] 将 `vision-service/` 移出仓库到 `E:\vision-service-wip-backup-20261126-190147`。
- [x] 保留 `docs/specs/2026-11-26-edgepilot-vision-spec.md` 作为唯一规格来源。
- [x] 确认工作树只剩 spec 未跟踪文件；无既有模块改动。

---

## Phase P2：vision-service

### Task 1：服务骨架、限额配置与模型状态

**Files:**
- Create: `vision-service/requirements.txt`
- Create: `vision-service/pyproject.toml`
- Create: `vision-service/app/__init__.py`
- Create: `vision-service/app/config.py`
- Create: `vision-service/app/schemas.py`
- Create: `vision-service/app/auth.py`
- Create: `vision-service/app/model.py`
- Test: `vision-service/tests/test_config_model.py`

**Interfaces:**
- Produces: `Settings`（`max_file_bytes=200*1024*1024`、`max_video_seconds=900`、`max_samples=2000`、`target_samples=900`、`job_timeout_s=1800`）、`Settings.from_env()`、`Settings.storage_writable() -> bool`。
- Produces: `ModelRegistry.info() -> dict`，字段含 `load_status`、`model_name/yolo11n`、`model_version`、`weights_sha256`、`license`、`commercial_use_notice`。
- Produces: `JobParams`、`JobCreate`、`ProbeIn`、`JobStateOut`；`JobCreate` 含 `task_uid`、`video_path`、`filename`、`device_id`、`video_sha256`。

- [ ] **Step 1：写失败测试**

```python
# vision-service/tests/test_config_model.py
from pathlib import Path
from app.config import Settings
from app.model import ModelRegistry

def test_default_limits():
    s = Settings()
    assert s.max_file_bytes == 200 * 1024 * 1024
    assert s.max_video_seconds == 900
    assert s.target_samples == 900
    assert s.max_samples == 2000
    assert s.job_timeout_s == 1800

def test_env_overrides(monkeypatch):
    monkeypatch.setenv("VISION_MAX_FILE_MB", "10")
    monkeypatch.setenv("VISION_MAX_SAMPLES", "1500")
    s = Settings.from_env()
    assert s.max_file_bytes == 10 * 1024 * 1024
    assert s.max_samples == 1500

def test_missing_weights_is_honest(tmp_path: Path):
    registry = ModelRegistry(Settings(model_path=tmp_path / "none.pt", preload=False))
    info = registry.info()
    assert info["load_status"] == "missing_weights"
    assert info["license"] == "AGPL-3.0"
    assert "商业" in info["commercial_use_notice"]

def test_storage_writable(tmp_path: Path):
    s = Settings(jobs_dir=tmp_path / "jobs")
    assert s.storage_writable() is True
    assert (tmp_path / "jobs" / ".health-write").exists() is False
```

- [ ] **Step 2：运行并确认失败**

Run: `cd vision-service && python -m pytest tests/test_config_model.py -q`
Expected: FAIL，`ModuleNotFoundError: app.config`。

- [ ] **Step 3：实现配置与模型登记**

```python
# vision-service/app/config.py（关键部分）
LICENSE_NAME = "AGPL-3.0"
LICENSE_URL = "https://www.ultralytics.com/license"
COMMERCIAL_NOTICE = (
    "Ultralytics YOLO 采用 AGPL-3.0 许可。将本服务用于闭源商业产品或对外提供"
    "网络服务前，请自行评估 AGPL 义务或购买 Ultralytics 商业许可；本仓库不默认"
    "授予商业使用权限。"
)

@dataclass(frozen=True)
class Settings:
    service_name: str = "edgepilot-vision"
    host: str = "0.0.0.0"
    port: int = 8001
    jobs_dir: Path = SERVICE_ROOT / "var" / "jobs"
    model_path: Path = SERVICE_ROOT / "weights" / "yolo11n.pt"
    device: str = "cpu"
    imgsz: int = 640
    iou: float = 0.45
    max_det: int = 100
    default_conf: float = 0.25
    token: str = ""
    preload: bool = True
    max_workers: int = 1
    max_pending: int = 32
    job_timeout_s: int = 1800
    max_file_bytes: int = 200 * 1024 * 1024
    max_video_seconds: int = 900
    max_samples: int = 2000
    target_samples: int = 900
    allowed_roots: tuple[Path, ...] = ()

    def storage_writable(self) -> bool:
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            probe = self.jobs_dir / ".health-write"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False
```

`ModelRegistry` 只做三件事：根据 `model_path` 判断 `ready/missing_weights/load_error`；懒加载 `ultralytics.YOLO`；计算权重 SHA-256 并把版本拼成 `yolo11n-<sha12>`。不得在缺权重时返回 ready。

- [ ] **Step 4：运行测试**

Run: `cd vision-service && python -m pytest tests/test_config_model.py -q`
Expected: `4 passed`。

### Task 2：标准化 Adapter（artifact 契约）

**Files:**
- Create: `vision-service/app/standardize.py`
- Test: `vision-service/tests/test_standardize.py`

**Interfaces:**
- Produces: `SCHEMA_VERSION = "vision-result/v1"`、`clamp_bbox(x1,y1,x2,y2,width,height)`、`make_detection(...)`、`make_frame(...)`、`summarize(frames)`、`build_artifact(...)`、`artifact_digest(artifact)`。
- `bbox` 固定 `{x1,y1,x2,y2}`，两位小数并裁到画面内。
- `detection_id = f"{frame_id}-{index}"`；`confidence` 四位小数。

- [ ] **Step 1：写失败测试**

```python
from app.standardize import build_artifact, clamp_bbox, make_detection, make_frame, summarize

def test_clamp_bbox_crops_to_frame():
    assert clamp_bbox(-5, 10, 700, 500, width=640, height=480) == {
        "x1": 0.0, "y1": 10.0, "x2": 640.0, "y2": 480.0,
    }

def test_summary_counts_empty_frames():
    frames = [
        make_frame(frame_id=0, timestamp_ms=0, inference_ms=10, detections=[
            make_detection(frame_id=0, index=0, class_id=0, class_name="person",
                           confidence=0.9, bbox={"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        ]),
        make_frame(frame_id=5, timestamp_ms=200, inference_ms=5, detections=[]),
    ]
    summary = summarize(frames)
    assert summary["processed_frames"] == 2
    assert summary["detection_count"] == 1
    assert summary["class_counts"] == {"person": 1}
    assert summary["peak_confidence"] == 0.9

def test_artifact_has_contract_fields():
    artifact = build_artifact(
        task_uid="task-1", device_id="video-upload", filename="a.mp4",
        model={"model_name": "yolo11n"}, video={"width": 10, "height": 10},
        params={"conf_threshold": 0.25}, frames=[], created_at="2026-11-26T00:00:00+00:00",
        finished_at="2026-11-26T00:00:01+00:00",
    )
    assert artifact["schema_version"] == "vision-result/v1"
    assert artifact["event_id"] == "task-1"
    assert artifact["error"] is None
    assert artifact["source"]["type"] == "video_file"
```

- [ ] **Step 2：实现标准化模块**

按 spec 第 9 节实现。关键要求：`detections=[]` 合法；不得用空数组掩盖失败；`artifact_digest` 使用 `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)` 的 SHA-256。

- [ ] **Step 3：运行测试**

Run: `cd vision-service && python -m pytest tests/test_standardize.py -q`
Expected: 全部通过。

### Task 3：probe、PTS 与 900/2000 抽帧

**Files:**
- Create: `vision-service/app/inference.py`
- Test: `vision-service/tests/test_inference_units.py`

**Interfaces:**
- Produces: `InferenceError(code, message)`、`JobCancelled`、`JobSpec(task_uid, video_path, filename, device_id, video_sha256, params, created_at)`、`InferenceEngine.run(spec, progress, cancelled)`、`YoloEngine`、`probe_video(path, settings) -> dict`。
- `probe_video` 返回 `filename,size_bytes,width,height,fps,frame_count,duration_ms,duration_known,max_video_seconds,max_samples,target_samples`；同时返回 `model_name`、`model_version`（来自 `ModelRegistry.info()`，用于上传指纹）。
- 错误码：`video_missing`、`empty_video`、`decode_failed`、`file_too_large`、`video_too_long`、`frame_limit_exceeded`、`timeout`、`inference_failed`。

- [ ] **Step 1：写假 cv2 测试**

测试注入 `sys.modules["cv2"]`，覆盖：
1. PTS 单调递增时 `timestamp_ms` 等于 PTS；
2. PTS 恒 0 时回退 FPS；
3. 2700 帧 + `target_samples=900` 时 `sampled_every_n_frames=3`、`processed_frames=900`；
4. `frame_count=0` 且 `max_samples=3` 时第 4 帧抛 `frame_limit_exceeded`；
5. `900s+1帧` 抛 `video_too_long`；
6. `probe_video` 返回真实宽高/FPS/时长；
7. 超过 `max_file_bytes` 抛 `file_too_large`。

```python
def test_target_samples_drives_stride(tmp_path, monkeypatch):
    capture = FakeCapture(fps=30.0, frame_count=2700)
    _install_fake_cv2(monkeypatch, capture)
    engine = YoloEngine(FakeRegistry(), _settings(tmp_path))
    artifact = engine.run(
        JobSpec(task_uid="task-stride", video_path=str(_video(tmp_path)),
                filename="clip.mp4", params={"target_samples": 900}),
        lambda _p: None, lambda: False,
    )
    assert artifact["video"]["sampled_every_n_frames"] == 3
    assert artifact["video"]["processed_frames"] == 900
```

- [ ] **Step 2：实现推理循环**

```python
# 关键顺序：先校验大小/时长，再算步长，再逐帧读 PTS
stride = requested_stride
if reported_frames > 0:
    stride = max(stride, math.ceil(reported_frames / target_samples))

while True:
    ok, frame = cap.read()
    if not ok:
        break
    raw_pts = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
    if math.isfinite(raw_pts) and raw_pts >= 0 and raw_pts > last_pts:
        timestamp_ms, last_pts = int(round(raw_pts)), raw_pts
        pts_used += 1
    else:
        timestamp_ms = int(round(index * 1000.0 / fps)) if fps > 0 else index * 40
        pts_fallbacks += 1

    if index % stride == 0:
        if processed >= settings.max_samples:
            raise InferenceError("frame_limit_exceeded", f"采样帧数超过硬上限 {settings.max_samples}")
        # model.predict(...) -> adapter -> make_frame(frame_id=index, timestamp_ms=...)
        processed += 1
    index += 1
```

`artifact["video"]` 必须写入 `timestamp_source`（`pts` / `fps` / `fallback_25fps`）、`max_samples`、`size_bytes`、`sha256`、`sampled_every_n_frames`、`processed_frames`。

- [ ] **Step 3：运行测试**

Run: `cd vision-service && python -m pytest tests/test_inference_units.py -q`
Expected: 8 passed。

### Task 4：任务状态持久化、原子结果与重启恢复

**Files:**
- Create: `vision-service/app/jobs.py`
- Test: `vision-service/tests/test_jobs.py`

**Interfaces:**
- Produces: `JobManager.submit(spec) -> dict`、`get(task_uid)`、`cancel(task_uid)`、`result(task_uid)`、`queue_stats()`、`shutdown()`。
- 状态文件 `<jobs_dir>/<uid>/state.json`；结果 `<jobs_dir>/<uid>/result.json`。
- `submit` 幂等：`pending/running/completed` 原样返回；`failed/cancelled` 新 attempt 并删除旧 artifact。
- 结果写盘顺序：临时文件 → `flush` → `os.fsync` → `os.replace`；成功后才写 `completed`。

- [ ] **Step 1：写测试**

```python
def test_completed_only_after_result_written(tmp_path):
    manager = JobManager(_settings(tmp_path), FakeEngine())
    state = manager.submit(_spec("task-ok"))
    state = _wait(manager, "task-ok", {"completed"})
    assert state["result_available"] is True
    assert state["result_sha256"]
    assert state["result_file_sha256"]
    assert state["result_bytes"] > 0
    assert state["result_schema_version"] == "vision-result/v1"

def test_recovery_marks_pending_and_running_failed(tmp_path):
    job_dir = _settings(tmp_path).jobs_dir / "task-stale"
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(
        '{"task_uid":"task-stale","status":"running","progress":40,"attempt":1}',
        encoding="utf-8",
    )
    manager = JobManager(_settings(tmp_path), FakeEngine())
    state = manager.get("task-stale")
    assert state["status"] == "failed"
    assert state["error_code"] == "service_restarted"

def test_retry_failed_job_increments_attempt(tmp_path):
    manager = JobManager(_settings(tmp_path), FailingEngine())
    manager.submit(_spec("task-retry"))
    _wait(manager, "task-retry", {"failed"})
    manager.submit(_spec("task-retry"))
    assert _wait(manager, "task-retry", {"failed"})["attempt"] == 2
```

- [ ] **Step 2：实现并按状态落盘**

`_write_json_atomic` 固定为：

```python
def _write_json_atomic(self, path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
```

`_run` 中必须先 `_write_json_atomic(result_path, artifact)`，再 `_update(status="completed", result_path=..., result_bytes=..., result_sha256=canonical, result_file_sha256=file_hash, result_schema_version=...)`；`InferenceError` 只写 `failed`，绝不写伪结果。`result_sha256` 用 `artifact_digest`，`result_file_sha256` 用文件字节 SHA-256，两者都写入 state。

- [ ] **Step 3：运行测试**

Run: `cd vision-service && python -m pytest tests/test_jobs.py -q`
Expected: 全部通过；`state.json` 中断恢复不丢已完成任务。

### Task 5：HTTP 契约（health / probe / jobs）

**Files:**
- Create: `vision-service/app/main.py`
- Test: `vision-service/tests/test_api.py`

**Interfaces:**
- `GET /health`：返回 `status`、`storage.writable`、`model`、`queue`；缺权重 `degraded` 但 HTTP 200。
- `GET /v1/models`、`POST /v1/models/reload`、`POST /v1/probe`、`POST /v1/jobs`（202）、`GET /v1/jobs/{uid}`、`POST /v1/jobs/{uid}/cancel`、`GET /v1/jobs/{uid}/results`。
- `InferenceError` → 413 (`file_too_large`)、422 (`video_too_long`/`frame_limit_exceeded`)、400 (`video_missing`/`empty_video`/`decode_failed`)、500 其它。
- `video_path` 必须绝对路径、文件存在、后缀白名单、在 `allowed_roots` 内。

- [ ] **Step 1：写契约测试**

```python
def test_health_without_token_reports_missing_weights(client):
    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["model"]["status"] == "missing_weights"
    assert body["storage"]["writable"] is True

def test_probe_maps_errors(tmp_path, monkeypatch):
    # 假 cv2 + max_file_bytes=16 -> 413
    response = client.post("/v1/probe", json={"video_path": str(video)})
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"

def test_job_lifecycle_and_results(client, tmp_path):
    video = _video(tmp_path)
    response = client.post("/v1/jobs", json={
        "task_uid": "task-0001", "video_path": str(video),
        "params": {"conf_threshold": 0.3, "target_samples": 900},
    })
    assert response.status_code == 202
    state = _wait(client, "task-0001", {"completed"})
    assert state["progress"] == 100
    assert state["result_available"] is True
    artifact = client.get("/v1/jobs/task-0001/results").json()
    assert artifact["schema_version"] == "vision-result/v1"
```

- [ ] **Step 2：实现异常映射**

```python
def _inference_error_status(code: str) -> int:
    if code == "file_too_large":
        return 413
    if code in {"video_too_long", "frame_limit_exceeded"}:
        return 422
    if code in {"video_missing", "empty_video", "decode_failed"}:
        return 400
    return 500

@app.exception_handler(InferenceError)
async def _inference_failed(_: Request, exc: InferenceError) -> JSONResponse:
    return JSONResponse(
        status_code=_inference_error_status(exc.code),
        content={"detail": exc.message, "code": exc.code},
    )
```

- [ ] **Step 3：运行契约测试**

Run: `cd vision-service && python -m pytest tests/test_api.py tests/test_standardize.py tests/test_inference_units.py tests/test_config_model.py tests/test_jobs.py -q`
Expected: 全部通过。

### Task 6：真实推理测试（门控）

**Files:**
- Create: `vision-service/tests/test_inference_real.py`
- Create: `vision-service/scripts/fetch_model.py`

- [ ] **Step 1：写真实推理测试**

```python
@pytest.mark.skipif(os.environ.get("RUN_REAL_YOLO") != "1", reason="set RUN_REAL_YOLO=1")
def test_real_video_detects_person(tmp_path):
    video = download_vtest(tmp_path)  # OpenCV vtest.avi，记录 URL 与 SHA-256
    settings = Settings(
        jobs_dir=tmp_path / "jobs", model_path=Path(os.environ.get("VISION_MODEL_PATH", "weights/yolo11n.pt")),
        preload=True, max_samples=2000, target_samples=900,
    )
    registry = ModelRegistry(settings)
    engine = YoloEngine(registry, settings)
    artifact = engine.run(
        JobSpec(task_uid="real-video-0001", video_path=str(video), filename=video.name,
                params={"conf_threshold": 0.25, "target_samples": 900}),
        lambda _p: None, lambda: False,
    )
    assert artifact["summary"]["processed_frames"] > 0
    assert artifact["summary"]["detection_count"] >= 1
    assert any(d["class_name"] == "person" for f in artifact["frames"] for d in f["detections"])
    assert artifact["model"]["license"] == "AGPL-3.0"
```

- [ ] **Step 2：下载权重与样例并执行**

`fetch_model.py` 固定从 Ultralytics 官方 release 下载 `yolo11n.pt` 到 `vision-service/weights/`，下载后打印文件大小与 SHA-256；样例使用 OpenCV 官方 `vtest.avi`（`https://raw.githubusercontent.com/opencv/opencv/master/samples/data/vtest.avi`），测试下载后同样打印 SHA-256 并写入交付文档，不在仓库硬编码不可核验的哈希。

Run:
```bash
cd vision-service
python scripts/fetch_model.py
RUN_REAL_YOLO=1 VISION_MODEL_PATH=weights/yolo11n.pt python -m pytest tests/test_inference_real.py -v
```
Expected: 1 passed；若网络/权重缺失，测试明确 skip/fail，不得伪造通过。`RUN_REAL_YOLO` 不等于 1 时该测试必须 skip，不能靠 Mock 冒充真实推理。

---

## Phase P3：backend 数据与任务链路

### Task 7：3 张新表、迁移与 manifest

**Files:**
- Create: `backend/app/models/vision.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/migrations/versions/0006_vision.py`
- Modify: `backend/tests/integration/test_schema_manifest.py`
- Test: `backend/tests/integration/test_schema_manifest.py`

**Interfaces:**
- `VisionTask`：任务状态、文件元数据、抽帧/模型/错误/轮询计数。
- `VisionResult`：artifact 指针、哈希、schema_version、事件数、工作流/Agent 关联与联动状态。
- `VisionModel`：模型登记与许可提示。
- 新迁移 `revision="0006_vision"`，`down_revision="0005_obs_analytics_indexes"`。

- [ ] **Step 1：先改 manifest 测试**

```python
EXPECTED_TABLES = {
    ..., "vision_tasks", "vision_results", "vision_models",
}
```

Run: `cd backend && python -m pytest tests/integration/test_schema_manifest.py -q`
Expected: FAIL，缺表。

- [ ] **Step 2：实现模型**

```python
class VisionTask(TenantMixin, TimestampMixin, Base):
    __tablename__ = "vision_tasks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_uid", name="uq_vision_tasks_tenant_uid"),
        UniqueConstraint("tenant_id", "dedup_key", name="uq_vision_tasks_tenant_dedup"),
        Index("ix_vision_tasks_tenant_created", "tenant_id", text("created_at DESC")),
        Index("ix_vision_tasks_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    task_uid: Mapped[str] = mapped_column(String(36), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="upload")
    video_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    video_path: Mapped[str] = mapped_column(String(512), nullable=False)
    video_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(64))
    conf_threshold: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, server_default="0.250")
    frame_stride: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    target_samples: Mapped[int] = mapped_column(Integer, nullable=False, server_default="900")
    zones: Mapped[list | None] = mapped_column(JSONB)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    processed_frames: Mapped[int | None] = mapped_column(Integer)
    detection_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    inference_ms: Mapped[int | None] = mapped_column(Integer)
    video_fps: Mapped[Decimal | None] = mapped_column(Numeric(7, 3))
    video_duration_ms: Mapped[int | None] = mapped_column(Integer)
    video_width: Mapped[int | None] = mapped_column(Integer)
    video_height: Mapped[int | None] = mapped_column(Integer)
    frame_count: Mapped[int | None] = mapped_column(Integer)
    sampled_every_n_frames: Mapped[int | None] = mapped_column(Integer)
    model_name: Mapped[str | None] = mapped_column(String(64))
    model_version: Mapped[str | None] = mapped_column(String(64))
    device: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(512))
    dispatch_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    poll_misses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
```

`VisionResult` 关键新增列：`task_uid String(36)`（与 `task_id` 双写，便于跨表关联与排障）、`artifact_bytes`、`result_sha256`（canonical）、`artifact_file_sha256`（文件字节）、`result_schema_version String(32)`、`event_count`、`workflow_run_id`、`trigger_status String(16) server_default="pending"`、`trigger_error String(255)`、`triggered_at`、`agent_run_id`、`agent_event_id BIGINT`（本次研判使用的事件）、`agent_status String(16) server_default="none"`、`agent_error String(512)`、`agent_trace_id String(32)`、`summary JSONB`、`purged_at`；唯一 `(tenant_id, task_id)`。

`VisionModel` 按 spec 7.3 实现；`classes JSONB`、`commercial_use_notice TEXT`、唯一 `(tenant_id, model_key)`。

- [ ] **Step 3：写迁移**

`0006_vision.py` 使用 `op.create_table` 创建三表、创建 4 个索引，并加工作流幂等唯一索引：

```python
op.execute("""
CREATE UNIQUE INDEX uq_workflow_runs_vision_task
ON workflow_runs ((trigger_payload->>'vision_task_uid'))
WHERE trigger_type = 'event'
  AND trigger_payload->>'vision_task_uid' IS NOT NULL
""")
```

downgrade 逆序 drop index/table。不得改既有列。

- [ ] **Step 4：运行迁移与 manifest**

Run:
```bash
cd backend
alembic upgrade head
python -m pytest tests/integration/test_schema_manifest.py -q
```
Expected: 3 张新表存在；manifest 通过。

### Task 8：配置、异常与 Pydantic 契约

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/core/exceptions.py`
- Create: `backend/app/schemas/vision.py`
- Modify: `backend/.env.example`
- Test: `backend/tests/unit/test_vision_schemas.py`

**Interfaces:**
- 新增 `PayloadTooLarge(AppError)`（413）、`InsufficientStorage(AppError)`（507）。
- `Settings` 新增 `vision_*` 字段（spec 12.1）+ `vision_task_timeout_s=1800`、`vision_poll_backoff_max_s=30`、`vision_model_key="yolo11n"`、`vision_agent_once_per_task=True`。
- `VisionZone`：`zone_id` 唯一、3–32 个点、坐标 0..1。
- `VisionZonesIn`：最多 10 个区域。
- `VisionTaskOut`、`VisionTaskListOut`、`VisionResultOut`（含 `task_uid`、`agent_event_id`）、`VisionEventOut`、`VisionOverviewOut`、`VisionHealthOut`。
- `VisionTaskDetailOut(VisionTaskOut)` 额外含 `has_video`、`artifact_purged`、`video_url`、`video_expires_at`、`result`（`VisionResultOut | None`）；列表仍用轻量 `VisionTaskOut`。
- `.env.example` 增加 spec 12.1 的全部 `VISION_*` 字段（含注释“推理服务不可用不影响其它模块”），不写入真实 token。

- [ ] **Step 1：写校验测试**

```python
def test_zone_requires_three_points_and_range():
    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=[[0.1, 0.1], [0.2, 0.2]])
    with pytest.raises(ValidationError):
        VisionZone(zone_id="gate", polygon=[[0, 0], [1, 1], [2, 0]])

def test_zone_id_unique():
    with pytest.raises(ValidationError):
        VisionZonesIn(zones=[
            VisionZone(zone_id="gate", polygon=[[0, 0], [1, 0], [1, 1]]),
            VisionZone(zone_id="gate", polygon=[[0, 0], [1, 0], [1, 1]]),
        ])

def test_limits_and_exceptions():
    s = Settings()
    assert s.vision_max_upload_mb == 200
    assert s.vision_max_video_seconds == 900
    assert s.vision_target_frames == 900
    assert s.vision_max_samples == 2000
    assert PayloadTooLarge.http_status == 413
    assert InsufficientStorage.http_status == 507
```

- [ ] **Step 2：实现配置与校验**

```python
class PayloadTooLarge(AppError):
    code = http_status = 413

class InsufficientStorage(AppError):
    code = http_status = 507
```

`VisionZone` 用 `field_validator("polygon")` 校验长度和范围；`VisionZonesIn` 用 `model_validator(mode="after")` 校验 `zone_id` 唯一。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/unit/test_vision_schemas.py -q`
Expected: 全部通过。

### Task 9：Repository、上传落盘与 artifact 读取

**Files:**
- Create: `backend/app/repositories/vision_repo.py`
- Create: `backend/app/services/vision_storage.py`
- Test: `backend/tests/unit/test_vision_storage.py`

**Interfaces:**
- `VisionRepo.get_task(tenant_id, task_uid, *, for_update=False)`；`get_by_dedup(tenant_id, dedup_key)`；`create_task(...)`；`list_tasks(...)`；`get_result(tenant_id, task_id, for_update=False)`；`upsert_result(tenant_id, **fields)`；`overview(tenant_id)`；`list_task_events(tenant_id, task_uid, page, size)`。
- `VisionStorage.save_upload(upload, tenant_id, task_uid) -> SavedUpload(sha256, size, ext, tmp_path, final_path)`；`validate_magic(head, ext)`；`move_into_place(saved)`；`resolve(relative_path)`；`remove(relative_path)`；`read_artifact(uid, expected_file_sha, expected_bytes, expected_schema) -> dict`；`artifact_digest(artifact) -> str`（与 vision-service 完全相同的 canonical JSON SHA-256）。
- 新增领域异常：`InvalidFileType`（400 `invalid_file_type`）、`ArtifactMissing`（404 `artifact_missing`）、`ArtifactCorrupted`（503 `artifact_corrupted`）、`ArtifactUnreadable`（422 `artifact_unreadable`）。它们继承 `AppError`，只新增不改既有异常。落盘 `OSError`（磁盘满/权限）统一转 `InsufficientStorage`（507 `storage_write_failed`），并删除临时文件。

- [ ] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_save_upload_streams_and_hashes(tmp_path):
    storage = VisionStorage(str(tmp_path))
    upload = SimpleUpload(b"RIFF" + b"\x00" * 4 + b"AVI " + b"x" * 64, "clip.avi")
    saved = await storage.save_upload(upload, TENANT, "task-1")
    assert saved.size == 72
    assert saved.sha256 == hashlib.sha256(upload.data).hexdigest()
    storage.validate_magic(upload.data[:32], ".avi")

def test_path_traversal_is_rejected(tmp_path):
    storage = VisionStorage(str(tmp_path))
    with pytest.raises(ValueError):
        storage.resolve("../../etc/passwd")

def test_wrong_magic_is_rejected(tmp_path):
    storage = VisionStorage(str(tmp_path))
    with pytest.raises(InvalidFileType):
        storage.validate_magic(b"not-a-video" + b"\x00" * 20, ".mp4")

def test_artifact_hash_mismatch_raises(tmp_path):
    # 写入 jobs/<uid>/result.json 后传错 sha
    with pytest.raises(ArtifactCorrupted):
        storage.read_artifact("task-1", "0" * 64, size, "vision-result/v1")
```

- [ ] **Step 2：实现流式上传与路径防护**

```python
async def save_upload(self, upload, tenant_id, task_uid):
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in ALLOWED_SUFFIXES:
        raise InvalidFileType("不支持的视频类型")
    tmp = self.temp_path(tenant_id, task_uid, ext)
    hasher = hashlib.sha256()
    size = 0
    head = b""
    try:
        with tmp.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > self.max_bytes:
                    raise PayloadTooLarge("视频超过 200MB", detail={"reason": "file_too_large"})
                if len(head) < 32:
                    head += chunk[: 32 - len(head)]
                hasher.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if size <= 0:
            raise InvalidFileType("视频文件为空", detail={"reason": "invalid_file_type"})
        self.validate_magic(head, ext)
        return SavedUpload(hasher.hexdigest(), size, ext, tmp, self.final_path(tenant_id, task_uid, ext))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
```

`resolve` 必须 `Path.resolve()` 后校验 `is_relative_to(root)`；`read_artifact` 先按文件字节计算 SHA-256 与大小，再解析 JSON 并校验 `schema_version`，任何不符抛 `ArtifactCorrupted`/`ArtifactMissing`/`ArtifactUnreadable`。文件字节哈希与 canonical JSON 哈希是两个字段，不能混用。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/unit/test_vision_storage.py -q`
Expected: 全部通过；超限临时文件被删除。

### Task 10：vision-service 客户端

**Files:**
- Create: `backend/app/services/vision_client.py`
- Test: `backend/tests/unit/test_vision_client.py`

**Interfaces:**
- `VisionClient(settings, transport=None)`；`health()`、`models()`、`probe(path)`、`create_job(task_uid, video_path, filename, sha, conf, target_samples, frame_stride)`、`get_job(uid)`、`cancel_job(uid)`。
- `frame_stride` 为 `None` 时请求体不得包含该键（vision-service 的 `JobParams.frame_stride` 是 1..300 的整数，显式 `null` 会 422）；不传时由 vision-service 使用默认步长。
- 所有方法超时 `vision_service_timeout_s`；连接/5xx 统一抛 `VisionServiceError(message, status)`；404 抛 `VisionJobNotFound`。

- [ ] **Step 1：用 httpx.MockTransport 写测试**

```python
@pytest.mark.asyncio
async def test_create_job_payload_and_token():
    seen = {}
    def handler(request):
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("x-vision-token")
        return httpx.Response(202, json={"task_uid": "t1", "status": "pending"})
    client = VisionClient(Settings(vision_service_token="secret"), transport=httpx.MockTransport(handler))
    state = await client.create_job("t1", "/data/v.mp4", "v.mp4", "a" * 64, 0.25, 900, 1)
    assert state["status"] == "pending"
    assert seen["token"] == "secret"

@pytest.mark.asyncio
async def test_network_error_maps_to_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)
    client = VisionClient(Settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(VisionServiceError):
        await client.health()
```

- [ ] **Step 2：实现客户端**

使用 `httpx.AsyncClient(timeout=settings.vision_service_timeout_s, transport=transport)`；`Content-Type: application/json`；`X-Vision-Token` 仅在配置非空时发送；错误响应读取 `detail`/`code` 生成可读消息；禁止记录完整 URL（只记录路径和 task_uid）。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/unit/test_vision_client.py -q`
Expected: 全部通过。

### Task 11：上传、指纹、并发幂等与派发

**Files:**
- Create: `backend/app/services/vision_task_service.py`
- Test: `backend/tests/integration/test_vision_upload.py`

**Interfaces:**
- `VisionTaskService.create_upload(file, *, conf_threshold, frame_stride, target_samples, zones, user, ip, user_agent) -> CreateResult(task, deduplicated)`。
- `fingerprint(tenant_id, sha256, model_key, model_version, conf, target_samples, frame_stride, zones) -> str`。
- `dispatch(task)`、`retry(uid, user)`、`cancel(uid, user)`、`detail(uid)`、`list(...)`。
- `retry` 只允许 `failed/cancelled`（`status="failed"` 且 `error_code` 可重试或用户主动重试），复用同一任务与视频，清空错误、`dispatch_attempts += 1`、重新派发，不创建新任务；`vision_max_retries` 为上限，超过 409 `retry_limit_exceeded`。重新上传同一文件属于新请求：若模型版本/参数变化会得到新指纹和新任务；完全相同的指纹仍返回既有任务（`deduplicated=true`）。
- 上传事务：临时文件 → probe（含 `model_version`）→ 计算有效步长与指纹 → 查重 → 原子移动 → 插入（唯一约束兜底）→ 派发。
- 数据库 `video_path` 存相对 `VISION_STORAGE_DIR` 的路径；派发前必须经 `VisionStorage.resolve()` 转绝对路径再传给 vision-service，禁止把用户输入直接当路径。
- 有效步长：`estimated = probe.frame_count or probe.duration_ms/1000*probe.fps`；`stride = max(requested or 1, ceil(estimated / target_samples))`，写入 `vision_tasks.frame_stride`，再由 vision-service 以 2000 硬上限二次收敛。
- vision 不可达：任务 `failed(vision_service_unavailable)`，HTTP 200，并保留上传文件供 `POST /vision/tasks/{uid}/retry` 重新派发。
- probe 失败发生在任务落库前：先用 `vision_model_key` + `model_version=""` 计算指纹再落 failed 任务，保证服务宕机期间重复上传不会产生大量重复失败任务；服务恢复后 probe 能取到真实 `model_version`，新上传因指纹不同会创建新任务。若任务已存在（同指纹），返回既有任务并标记 `deduplicated=true`。
- 超过 200MB → 413 `file_too_large`；磁盘失败 → 507 `storage_write_failed`；类型/魔数 → 400 `invalid_file_type`；时长 >900s → 422 `video_too_long`。
- 派发结果只影响任务状态，不影响上传响应：派发失败时任务仍返回 200 + `status="failed"`，由 retry 端点受控重试。

- [ ] **Step 1：写集成测试**

```python
@pytest.mark.asyncio
async def test_duplicate_upload_returns_existing(client, admin_token, monkeypatch):
    fake_vision.ok = True
    data = _valid_mp4()
    first = await client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                              files={"file": ("gate.mp4", data, "video/mp4")})
    second = await client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                               files={"file": ("gate.mp4", data, "video/mp4")})
    assert first.json()["data"]["deduplicated"] is False
    assert second.json()["data"]["deduplicated"] is True
    assert first.json()["data"]["task"]["task_uid"] == second.json()["data"]["task"]["task_uid"]

@pytest.mark.asyncio
async def test_concurrent_same_file_has_one_task(client, admin_token, monkeypatch):
    responses = await asyncio.gather(*[
        client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                    files={"file": ("gate.mp4", _valid_mp4(), "video/mp4")})
        for _ in range(2)
    ])
    uids = {r.json()["data"]["task"]["task_uid"] for r in responses}
    assert len(uids) == 1

@pytest.mark.asyncio
async def test_vision_down_creates_failed_task(client, admin_token, monkeypatch):
    fake_vision.ok = False
    r = await client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                          files={"file": ("gate.mp4", _valid_mp4(), "video/mp4")})
    assert r.status_code == 200
    assert r.json()["data"]["task"]["status"] == "failed"
    assert r.json()["data"]["task"]["error_code"] == "vision_service_unavailable"

@pytest.mark.asyncio
async def test_vision_down_duplicate_upload_is_deduplicated(client, admin_token, monkeypatch):
    fake_vision.ok = False
    first = await client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                              files={"file": ("gate.mp4", _valid_mp4(), "video/mp4")})
    second = await client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                               files={"file": ("gate.mp4", _valid_mp4(), "video/mp4")})
    assert second.json()["data"]["deduplicated"] is True
    assert first.json()["data"]["task"]["task_uid"] == second.json()["data"]["task"]["task_uid"]

@pytest.mark.asyncio
async def test_retry_reuses_task_after_vision_recovers(client, admin_token, monkeypatch):
    fake_vision.ok = False
    first = await client.post("/api/v1/vision/tasks", headers=auth_headers(admin_token),
                              files={"file": ("gate.mp4", _valid_mp4(), "video/mp4")})
    uid = first.json()["data"]["task"]["task_uid"]
    fake_vision.ok = True
    retried = await client.post(f"/api/v1/vision/tasks/{uid}/retry", headers=auth_headers(admin_token))
    assert retried.json()["data"]["task"]["task_uid"] == uid
    assert retried.json()["data"]["task"]["dispatch_attempts"] == 1
```

- [ ] **Step 2：实现指纹与插入竞争**

```python
def fingerprint(tenant_id, sha256, model_key, model_version, conf, target_samples, frame_stride, zones):
    canonical = json.dumps(
        {
            "tenant_id": str(tenant_id),
            "video_sha256": sha256,
            "model": model_key,
            "model_version": model_version,
            "conf": round(float(conf), 3),
            "target_samples": int(target_samples),
            "frame_stride": int(frame_stride or 0),
            "zones": zones or [],
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

插入任务：

```python
try:
    task = VisionTask(...)
    session.add(task)
    await session.commit()
except IntegrityError:
    await session.rollback()
    existing = await repo.get_by_dedup(tenant_id, dedup_key)
    storage.remove(saved.relative_path)
    if existing is None:
        raise Conflict("重复上传竞争失败，请重试", detail={"reason": "upload_conflict"})
    return CreateResult(existing, True)
```

两个并发请求使用不同 `task_uid` 和最终文件名，失败方删除自己的文件，不覆盖赢家。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_upload.py -q`
Expected: 重复/并发上传只有一个任务；vision down 返回 failed 任务；超限 413。

### Task 12：artifact 入库与「先结果、后完成」

**Files:**
- Create: `backend/app/services/vision_result_service.py`
- Test: `backend/tests/integration/test_vision_completion.py`

**Interfaces:**
- `VisionResultService.ingest(task, remote_state) -> VisionResult`。
- 流程：`read_artifact` 校验（文件字节 SHA-256 + canonical SHA-256 + schema_version）→ `upsert_result` + commit → 事件生成 + commit → `task.status="completed"` + commit → **触发联动**（见 Task 17）→ 更新 `trigger_status`。
- 失败：`artifact_missing` / `artifact_corrupted` / `artifact_unreadable` → 任务 failed，**不得**写空结果。
- `VisionResult` 同时保存 `result_sha256`（canonical）与 `artifact_file_sha256`（文件字节）；大小、哈希、schema_version 缺一不可。
- 重放：同一 artifact 重复 ingest 只更新结果行，不重复事件、不重复完成；联动由 Task 17 的任务级幂等兜底。
- 联动必须发生在 `completed` 提交之后；`VisionTriggerService.trigger` 的任何异常只写 `trigger_status="failed"` / `trigger_error`，不得把任务改回 failed，也不得回滚已落库的结果。

- [ ] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_completed_only_after_result_commit(session, fake_artifact):
    task = await _make_task(session)
    result = await VisionResultService(session).ingest(task, fake_artifact)
    assert result.frame_count == 2
    await session.refresh(task)
    assert task.status == "completed"
    assert task.detection_count == 1

@pytest.mark.asyncio
async def test_bad_hash_fails_task_without_empty_result(session, fake_artifact):
    task = await _make_task(session)
    fake_artifact.sha256 = "0" * 64
    with pytest.raises(ArtifactCorrupted):
        await VisionResultService(session).ingest(task, fake_artifact)
    assert await _result_count(session, task.id) == 0
```

- [ ] **Step 2：实现顺序**

```python
artifact = storage.read_artifact(uid, state["result_file_sha256"], state["result_bytes"], state["result_schema_version"])
canonical_sha = artifact_digest(artifact)
if canonical_sha != state["result_sha256"]:
    raise ArtifactCorrupted("artifact canonical 哈希不符")
frames = artifact.get("frames")
if not isinstance(frames, list) or not frames:
    raise ArtifactUnreadable("artifact 缺少 frames，拒绝以空结果完成")
# frames 为空表示没有采样结果，属于失败；frames 非空但 detections=[] 是合法的“无目标”，
# detection_count=0 可以完成，不得把两者混为一谈。
result = await repo.upsert_result(tenant_id, task_id=task.id, task_uid=task.task_uid, ...)
await session.commit()                  # 关键：结果先落库
event_count = await events.generate(task, artifact)
await session.commit()
task.status = "completed"
task.progress = 100
task.finished_at = now
await session.commit()                  # 最后才 COMPLETED

outcome = await VisionTriggerService(session).trigger(task, result)
await session.commit()                  # 联动状态单独提交；失败不影响 completed
```

注意：`upsert_result` 采用「先查后写 + `(tenant_id, task_id)` 唯一约束兜底」，冲突时回滚并重查（与现有 `VisionRepo.upsert_result` 一致），不依赖特定 PostgreSQL ON CONFLICT 语法；事件生成内部按 `dedup_key` 冲突跳过。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_completion.py -q`
Expected: 完成顺序、哈希失败、重放幂等全部通过。

### Task 13：事件规则、5s 冷却与确定性 dedup_key

**Files:**
- Create: `backend/app/services/vision_event_service.py`
- Test: `backend/tests/unit/test_vision_rules_events.py`

**Interfaces:**
- `build_event_candidates(task, artifact) -> list[dict]`（纯函数，便于冷却/区域单测）与 `generate(task, artifact) -> int`（写库并返回新建事件数）。
- `vision.detection`：每帧最高置信度 person，`confidence >= vision_event_conf`；同任务同类型 5s 冷却。
- `security.zone_intrusion`：person bbox 中心点落在归一化 polygon 内；每 zone 独立 5s 冷却；`severity="high"`。
- `dedup_key = f"vision:{task_uid}:{event_type}:{frame_id}:{class_id}"`，区域追加 `:zone:{zone_id}`；截断 128。
- `bbox` 转 `{x,y,w,h}`；`device_id/node_id=None`；`payload.simulated=False`。

- [ ] **Step 1：写测试**

```python
def test_cooldown_keeps_later_segment(task):
    art = _artifact([(0, 1000, 0.9), (2000, 3000, 0.9), (5000, 6000, 0.9)])
    events = build_event_candidates(task, art)
    assert [e["timestamp_ms"] for e in events] == [0, 5000]   # 2s 被合并，5s 后重新产生

def test_zone_intrusion_generates_high_event(task_with_zone):
    art = _artifact([(0, 1000, 0.9)], bbox=(10, 10, 100, 100), width=200, height=200)
    events = build_event_candidates(task_with_zone, art)
    zone = [e for e in events if e["event_type"] == "security.zone_intrusion"]
    assert len(zone) == 1
    assert zone[0]["severity"] == "high"
    assert zone[0]["payload"]["zone_id"] == "gate"

def test_dedup_key_is_deterministic(task):
    a = dedup_for(task.task_uid, "vision.detection", 12, 0, None)
    b = dedup_for(task.task_uid, "vision.detection", 12, 0, None)
    assert a == b
```

- [ ] **Step 2：实现生成与入库**

```python
last_seen: dict[str, int] = {}
for frame in artifact["frames"]:
    persons = [d for d in frame["detections"]
               if d["class_name"] == "person" and float(d["confidence"]) >= threshold]
    if not persons:
        continue
    best = max(persons, key=lambda d: float(d["confidence"]))
    key = "vision.detection"
    if frame["timestamp_ms"] - last_seen.get(key, -10**9) >= cooldown_ms:
        last_seen[key] = frame["timestamp_ms"]
        candidates.append(_event(task, frame, best, "vision.detection", "medium", None))
    for zone in task.zones or []:
        if point_in_polygon(_center(best["bbox"]), zone["polygon"]):
            zkey = f"security.zone_intrusion:{zone['zone_id']}"
            if frame["timestamp_ms"] - last_seen.get(zkey, -10**9) >= cooldown_ms:
                last_seen[zkey] = frame["timestamp_ms"]
                candidates.append(_event(task, frame, best, "security.zone_intrusion", "high", zone))
```

写入使用 `insert(VisionEvent).values(...).on_conflict_do_nothing(index_elements=["tenant_id", "dedup_key"]).returning(VisionEvent.id)`；统计实际返回行数。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/unit/test_vision_rules_events.py -q`
Expected: 冷却、区域、dedup 全部通过。

### Task 14：轮询器（2s + 指数退避 + 超时 + 恢复）

**Files:**
- Create: `backend/app/services/vision_sync.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/integration/test_vision_sync.py`

**Interfaces:**
- `VisionSyncPoller(session_factory, client, settings)`；`start()`、`stop()`、`tick()`。
- 每 `vision_poll_interval_s=2` 扫描 `status in (pending, running)`。
- `delay_for(task) -> int`：`min(vision_poll_interval_s * 2**poll_failures, vision_poll_backoff_max_s)`；成功清零。
- 每任务失败计数 `poll_failures`；下次轮询延迟按 `delay_for`；成功清零。
- 连续失败 30 次 → `failed(vision_service_unavailable)`；连续 404 3 次 → `failed(vision_job_lost)`。
- 任务创建超过 `vision_task_timeout_s=1800` 仍未终态 → 调 cancel + `failed(timeout)`。
- `completed` → `VisionResultService.ingest`（内部按「结果 → 事件 → 完成」顺序提交），随后由 Task 17 的任务级幂等触发工作流；`failed/cancelled` 复制错误；终态不再轮询。
- 启动恢复：`pending` 且 `dispatch_attempts=0` 立即派发；其余交给对账。启动补派发失败只记录日志，任务交由后续轮询/超时处理，不阻断 API 启动。

- [ ] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_terminal_task_not_polled_again(poller, fake_client):
    await _seed_task(status="completed")
    await poller.tick()
    assert fake_client.get_calls == 0

@pytest.mark.asyncio
async def test_backoff_grows_and_caps(poller, fake_client):
    task = await _seed_task(status="running")
    fake_client.fail = True
    await poller.tick()
    await poller.tick()
    assert task.poll_failures == 2
    assert poller.delay_for(task) == 8   # 2 * 2^2
    task.poll_failures = 10
    assert poller.delay_for(task) == 30

@pytest.mark.asyncio
async def test_404_marks_job_lost_after_three(poller, fake_client):
    task = await _seed_task(status="running")
    fake_client.not_found = True
    for _ in range(3):
        await poller.tick()
    assert task.status == "failed"
    assert task.error_code == "vision_job_lost"

@pytest.mark.asyncio
async def test_timeout_cancels_remote(poller, fake_client):
    task = await _seed_task(status="running", created_at=now - timedelta(seconds=1801))
    await poller.tick()
    assert fake_client.cancel_calls == 1
    assert task.error_code == "timeout"
```

- [ ] **Step 2：实现轮询与 lifespan**

```python
async def tick(self) -> None:
    now = datetime.now(timezone.utc)
    async with self.session_factory() as session:
        tasks = await VisionRepo(session).list_active()
        for task in tasks:
            set_tenant_id(task.tenant_id)
            try:
                if now - task.created_at > timedelta(seconds=self.settings.vision_task_timeout_s):
                    await self._timeout(task)
                    continue
                if task.last_polled_at and now < task.last_polled_at + timedelta(seconds=self.delay_for(task)):
                    continue
                await self._sync_one(session, task)
            finally:
                reset_tenant_id(token)
```

`main.py` lifespan 中仅当 `settings.vision_enabled` 时 `poller.start()`；`finally` 中 `await poller.stop()`；启动异常只记录日志，不阻断 API。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_sync.py -q`
Expected: 退避、404、超时、终态停止全部通过。

### Task 15：媒体 HMAC 签名与 Range 流

**Files:**
- Create: `backend/app/services/vision_media.py`
- Test: `backend/tests/unit/test_vision_media.py`

**Interfaces:**
- `sign_media_token(tenant_id, task_uid, file_id, *, ttl_s=3600, now=None) -> MediaToken(token, expires_at)`；`file_id` 传任务视频的 SHA-256，签名绑定租户 + 任务 + 文件，三者任一不匹配都 403。
- `verify_media_token(token, expires, tenant_id, task_uid, file_id, *, now=None) -> None`；失败抛 `Forbidden`。
- `parse_range(header, size) -> ByteRange | None`；语法错误/越界抛 `RangeNotSatisfiable`（416，继承 `AppError`）。
- `stream_video(path, range) -> Response`：无 Range 时 200 全量；合法 Range 返回 206 + `Content-Range`；非法 Range 由路由捕获并返回 `Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})`，不能只靠全局 JSON handler（不会带该响应头）。
- 签名 URL 形态固定为 `/api/v1/vision/tasks/{task_uid}/video?expires=<epoch>&token=<payload>.<sig>`；token payload 为 `{"t": tenant_id, "u": task_uid, "f": video_sha256, "e": expires}`，校验时同时比对 query `expires` 与 payload `e`、签名、租户、任务、文件 SHA-256，并检查未过期。详情响应返回 `video_url` 与 `video_expires_at`，不把 token 或 URL 写入日志。
- 响应头固定 `Accept-Ranges: bytes`、`Cache-Control: private, max-age=3600`、`Content-Disposition: inline`。

- [ ] **Step 1：写测试**

```python
def test_sign_verify_and_ttl():
    token = sign_media_token(TENANT, "task-1", "a" * 64, ttl_s=3600)
    verify_media_token(token.token, token.expires_at, TENANT, "task-1", "a" * 64)
    with pytest.raises(Forbidden):
        verify_media_token(token.token, token.expires_at, OTHER_TENANT, "task-1", "a" * 64)
    with pytest.raises(Forbidden):
        verify_media_token(token.token, token.expires_at, TENANT, "task-1", "b" * 64)

def test_expired_and_tampered_tokens_rejected():
    token = sign_media_token(TENANT, "task-1", "a" * 64, ttl_s=1, now=now - timedelta(seconds=2))
    with pytest.raises(Forbidden):
        verify_media_token(token.token, token.expires_at, TENANT, "task-1", "a" * 64)

def test_range_parsing():
    assert parse_range("bytes=0-1023", 5000) == (0, 1023)
    assert parse_range("bytes=4000-", 5000) == (4000, 4999)
    assert parse_range("bytes=-500", 5000) == (4500, 4999)
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=6000-7000", 5000)
```

- [ ] **Step 2：实现签名与流式响应**

```python
key = hmac.new(settings.jwt_secret.encode(), b"vision-media", hashlib.sha256).digest()
payload = base64.urlsafe_b64encode(json.dumps(
    {"t": str(tenant_id), "u": task_uid, "f": file_id, "e": expires_epoch},
    separators=(",", ":"),
).encode()).rstrip(b"=").decode()
sig = base64.urlsafe_b64encode(hmac.new(key, payload.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
token = f"{payload}.{sig}"
```

校验：分别比较 token 的 payload 与 query `expires` 一致、签名 `hmac.compare_digest`、`e >= now`、`t/u/f` 匹配。日志只记录 `task_uid`，不记录 URL/token。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/unit/test_vision_media.py -q`
Expected: 全部通过。

### Task 16：Vision API（14 个路由）与权限

**Files:**
- Create: `backend/app/api/v1/vision.py`
- Modify: `backend/app/api/v1/router.py`
- Test: `backend/tests/integration/test_vision_api.py`

**Interfaces:** 所有响应 `ok(...)`；分页 `PageParams`。

| 方法 | 路径 | 角色 |
| --- | --- | --- |
| GET | `/vision/health` | 登录 |
| GET | `/vision/overview` | 登录 |
| GET | `/vision/models` | 登录 |
| POST | `/vision/tasks` | admin/operator |
| GET | `/vision/tasks` | 登录 |
| GET | `/vision/tasks/{uid}` | 登录 |
| GET | `/vision/tasks/{uid}/results` | 登录 |
| POST | `/vision/tasks/{uid}/cancel` | admin/operator |
| POST | `/vision/tasks/{uid}/retry` | admin/operator |
| POST | `/vision/tasks/{uid}/trigger` | admin/operator（联动受控重试） |
| GET | `/vision/tasks/{uid}/events` | 登录 |
| POST | `/vision/tasks/{uid}/analyze` | admin/operator |
| POST | `/vision/events/{event_id}/analyze` | admin/operator |
| GET | `/vision/tasks/{uid}/video` | 签名（无 Bearer） |

`GET /vision/tasks/{uid}` 与 `GET /vision/tasks/{uid}/results` 额外返回媒体状态：`has_video`（文件存在）、`artifact_purged`（清理过）、`video_url` / `video_expires_at`（仅详情）；列表不返回签名 URL，避免签名泄漏到日志或缓存。`vision_enabled=false` 时 `/vision/*` 统一 503 `vision_disabled`，不影响其它路由。

- [ ] **Step 1：写权限与跨租户测试**

```python
@pytest.mark.asyncio
async def test_viewer_cannot_upload(client, viewer_token):
    r = await client.post("/api/v1/vision/tasks", headers=auth_headers(viewer_token),
                          files={"file": ("gate.mp4", _valid_mp4(), "video/mp4")})
    assert r.status_code == 403

@pytest.mark.asyncio
async def test_cross_tenant_task_is_404(client, other_tenant_token, completed_task):
    r = await client.get(f"/api/v1/vision/tasks/{completed_task.task_uid}",
                         headers=auth_headers(other_tenant_token))
    assert r.status_code == 404

@pytest.mark.asyncio
async def test_video_requires_signature(client, completed_task):
    assert (await client.get(f"/api/v1/vision/tasks/{completed_task.task_uid}/video")).status_code == 403
    detail = await client.get(f"/api/v1/vision/tasks/{completed_task.task_uid}", headers=...)
    url = detail.json()["data"]["video_url"]
    assert (await client.get(url)).status_code == 200
    assert (await client.get(url, headers={"Range": "bytes=0-1023"})).status_code == 206
    bad = await client.get(url, headers={"Range": "bytes=999999999-"})
    assert bad.status_code == 416
    assert bad.headers["content-range"] == "bytes */<size>"
```

- [ ] **Step 2：实现路由**

上传路由签名为：

```python
@router.post("/tasks")
async def create_task(
    file: UploadFile = File(...),
    conf_threshold: float = Form(0.25),
    frame_stride: int | None = Form(None),
    target_samples: int = Form(900),
    zones: str | None = Form(None),
    user: User = Depends(require_role("admin", "operator")),
    session: AsyncSession = Depends(get_db),
) -> dict:
    parsed_zones = VisionZonesIn.model_validate_json(zones).zones if zones else None
    result = await VisionTaskService(session).create_upload(...)
    return ok({...})
```

`video` 路由不得依赖 `get_current_user`，只校验签名；`StreamingResponse` 不打印 URL。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_api.py -q`
Expected: 权限、跨租户、签名、413/507 全部通过。

### Task 17：工作流任务级幂等触发

**Files:**
- Create: `backend/app/services/vision_trigger_service.py`
- Modify: `backend/app/repositories/workflow_repo.py`
- Test: `backend/tests/integration/test_vision_trigger.py`

**Interfaces:**
- `WorkflowRepo.find_published_by_name(tenant_id, name) -> Workflow | None`。
- `VisionTriggerService.trigger(task, result) -> TriggerOutcome(status, workflow_run_id, error)`。
- `trigger_status`：`pending/triggered/skipped/failed`。
- 条件：任务 completed 且存在事件；工作流名匹配且 published。
- 幂等：先查 `workflow_runs` 中 `trigger_type='event'` 且 `trigger_payload->>'vision_task_uid'=uid`；DB 唯一索引兜底。已有 Run 时直接返回 `triggered` 和既有 id，不重复创建。
- 调用点：`VisionResultService.ingest` 在任务 `completed` 提交之后调用；轮询重复收到 completed 时再次调用也只命中幂等分支。`VisionTriggerService` 失败只更新 `trigger_status="failed"` / `trigger_error`，不抛到轮询器、不回退任务。
- 失败不回退任务状态；`trigger_error` 保留原因；`POST /vision/tasks/{uid}/trigger` 仅允许对 `trigger_status in ("failed","skipped")` 的任务受控重试，`triggered` 再次调用仍返回既有 Run。
- `WorkflowRepo.find_published_by_name` 先 `get_by_name`，再校验 `status=="published"` 且 `current_version >= 1`；未发布返回 `None`，由服务记录 `skipped`。

- [ ] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_trigger_creates_one_event_run(session, completed_task_with_events):
    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "triggered"
    runs = await _runs_for(task.task_uid)
    assert len(runs) == 1
    assert runs[0].trigger_type == "event"
    assert runs[0].trigger_payload["vision_task_uid"] == task.task_uid

@pytest.mark.asyncio
async def test_replay_and_duplicate_poll_do_not_create_second_run(session, completed_task_with_events):
    service = VisionTriggerService(session)
    await service.trigger(task, result)
    await service.trigger(task, result)
    assert len(await _runs_for(task.task_uid)) == 1

@pytest.mark.asyncio
async def test_failure_is_recorded_without_changing_task(session, completed_task_with_events):
    # 删除已发布版本模拟 Create 失败
    outcome = await VisionTriggerService(session).trigger(task, result)
    assert outcome.status == "failed"
    assert outcome.error
    assert task.status == "completed"
```

- [ ] **Step 2：实现触发**

```python
existing = await session.scalar(
    select(WorkflowRun).where(
        WorkflowRun.tenant_id == get_tenant_id(),
        WorkflowRun.trigger_type == "event",
        WorkflowRun.trigger_payload["vision_task_uid"].astext == task.task_uid,
    )
)
if existing is not None:
    return TriggerOutcome("triggered", existing.id, None)

workflow = await WorkflowRepo(session).find_published_by_name(tenant_id, settings.vision_trigger_workflow_name)
if workflow is None:
    return TriggerOutcome("skipped", None, "未找到已发布工作流")
run = await RunService(session).create(
    workflow.id,
    input_data={"task_uid": task.task_uid, "video_filename": task.video_filename,
                "event_ids": event_ids, "event_count": len(event_ids),
                "summary": result.summary},
    trigger_type="event",
    trigger_payload={"source": "vision", "vision_task_id": task.id,
                     "vision_task_uid": task.task_uid, "vision_event_ids": event_ids,
                     "first_event_id": event_ids[0] if event_ids else None},
    user=None,
)
```

`RunService.create` 提交后返回 `RunContext`，取其中的 run id；若唯一索引冲突，回滚事务后按 `vision_task_uid` 回查既有 Run 并视为成功。`trigger_status` 的写入与 `workflow_run_id` 必须在同一 commit 内完成，避免出现“Run 已建但结果表无指针”的半状态。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_trigger.py -q`
Expected: 一次触发、重放不重复、失败保留状态。

### Task 18：Vision Agent 研判（每任务一次 + 受控重试）

**Files:**
- Create: `backend/app/services/vision_agent_service.py`
- Test: `backend/tests/integration/test_vision_agent.py`

**Interfaces:**
- `VisionAgentService.analyze_task(task_uid, user) -> dict`；`analyze_event(event_id, user) -> dict`。
- 选择租户内第一个 `agent_type='vision'` 且 `status='active'` 的 Agent；无则 409 `vision_agent_missing`。
- 任务无事件 → 409 `no_vision_events`。
- 输入只含任务摘要 + 单条事件 + 模型/视频元数据，**不逐帧调用 LLM**；不得把 `artifact["frames"]` 整段塞进 task。
- `vision_results.agent_status`：`none/running/succeeded/failed`；`succeeded` 时直接返回既有 `agent_runs` 响应，不新建 run；`failed` 允许重试。`POST /vision/events/{event_id}/analyze` 同样落到该事件所属任务，使用指定事件作为 `event` 字段，但仍遵守“每任务成功一次”的复用规则。
- Agent 失败不改变 `vision_tasks.status`；只更新 `agent_status/agent_error/agent_run_id/agent_trace_id`。
- 崩溃恢复：`agent_status="running"` 且结果行 `updated_at` 超过 `vision_task_timeout_s` 时，允许将状态改为 `failed(agent_interrupted)` 后重试，避免永久卡在 running；未超时仍返回 409 `vision_agent_running`。
- 返回字段与 `/agents/{id}/test` 完全一致：`id, agent_id, agent_version, status, output, error, trace_id, span_id, total_tokens, total_cost, retry_count`。

- [ ] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_analyze_uses_summary_not_frames(session, completed_task_with_events, fake_llm):
    response = await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    assert response["status"] == "succeeded"
    prompt = json.dumps(fake_llm.last_task)
    assert task.task_uid in prompt
    assert "frames" not in prompt
    assert "summary" in prompt

@pytest.mark.asyncio
async def test_second_analyze_reuses_run(session, completed_task_with_events, fake_llm):
    service = VisionAgentService(session)
    first = await service.analyze_task(task.task_uid, user=admin)
    second = await service.analyze_task(task.task_uid, user=admin)
    assert first["id"] == second["id"]
    assert fake_llm.calls == 1

@pytest.mark.asyncio
async def test_agent_failure_does_not_change_task_status(session, completed_task_with_events, failing_llm):
    response = await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    assert response["status"] == "failed"
    assert task.status == "completed"
    assert result.agent_status == "failed"

@pytest.mark.asyncio
async def test_agent_missing_returns_409(client, admin_token, completed_task_with_events):
    r = await client.post(f"/api/v1/vision/tasks/{task.task_uid}/analyze",
                          headers=auth_headers(admin_token))
    assert r.status_code == 409
    assert r.json()["data"]["reason"] == "vision_agent_missing"
```

- [ ] **Step 2：实现一次性研判**

```python
async def analyze_task(self, task_uid: str, *, user) -> dict:
    task, result = await self._load_task_result(task_uid, for_update=True)
    if result.agent_status == "succeeded" and result.agent_run_id:
        return await self._agent_run_response(result.agent_run_id)
    if result.agent_status == "running":
        if not self._running_expired(result):
            raise Conflict("研判正在进行", detail={"reason": "vision_agent_running"})
        result.agent_status = "failed"
        result.agent_error = "agent_interrupted"
    event = await self._latest_event(task)
    if event is None:
        raise Conflict("任务没有可研判事件", detail={"reason": "no_vision_events"})
    agent = await self._select_agent()
    result.agent_status = "running"
    await self.session.commit()

    payload = {
        "task": {"task_uid": task.task_uid, "video_filename": task.video_filename,
                 "summary": result.summary, "model": task.model_name,
                 "model_version": task.model_version, "video": {...}},
        "event": {"event_type": event.event_type, "severity": event.severity,
                  "confidence": float(event.confidence or 0), "occurred_at": event.occurred_at.isoformat(),
                  "bbox": event.bbox, "payload": event.payload},
        "instruction": "基于任务摘要与单条事件给出研判结论，不要伪造未提供的帧级信息。",
    }
    run = await run_agent(agent, payload, tracer=get_tracer(), llm=self._llm(agent), session=self.session)
    result.agent_status = run.status
    result.agent_run_id = run.agent_run_id
    result.agent_event_id = event.id
    result.agent_error = run.error
    result.agent_trace_id = run.trace_id
    await self.session.commit()
    return self._to_response(agent, run)
```

关联字段落点固定为：`vision_results.task_id` / `task_uid`、`vision_results.agent_event_id`、`vision_results.workflow_run_id`（工作流触发在 Task 17 写）、`vision_results.agent_run_id`、`vision_results.agent_trace_id`；事件侧用 `vision_events.payload.task_uid`，工作流侧用 `workflow_runs.trigger_payload.vision_task_uid`。这些字段共同构成验收要求的 `task_id/event_id/workflow_run_id/agent_run_id/trace_id` 证据链。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_agent.py -q`
Expected: 摘要输入、一次复用、失败不改任务、缺 Agent 409。

### Task 19：清理脚本与审计

**Files:**
- Create: `backend/scripts/vision_cleanup.py`
- Create: `backend/app/services/vision_cleanup_service.py`
- Test: `backend/tests/integration/test_vision_cleanup.py`

**Interfaces:**
- `python scripts/vision_cleanup.py --days 90 --actor <email> [--dry-run] [--purge-db] [--confirm]`；`--purge-db` 必须同时带 `--confirm`，否则拒绝执行。
- 权限控制：脚本只允许通过具备数据库写权限的运维账号执行；`--actor` 必填并写入审计 `vision.cleanup`（`actor`、`task_uid`、`deleted_files`、`dry_run`、`purge_db`）。HTTP 不提供清理接口，避免普通业务角色误删。
- 默认删除视频与 `jobs/<uid>/`；保留 `vision_tasks` 汇总与任务状态（默认为 `completed`，绝不因清理改成 failed）；`vision_results.artifact_path=None`、`purged_at=now`。
- `--purge-db` 删除 `vision_results` 行并把任务标 `failed(purged)`；这是显式破坏性操作，必须同时带 `--confirm`。
- 清理后详情返回 `video_missing=true`、`artifact_purged=true`，不得返回空 frames。

- [ ] **Step 1：写测试**

```python
@pytest.mark.asyncio
async def test_cleanup_removes_files_keeps_summary(session, tmp_path):
    task, result = await _expired_task(session, tmp_path)
    stats = await VisionCleanupService(session, str(tmp_path)).run(days=90, dry_run=False)
    assert stats.deleted_files == 2
    assert not Path(tmp_path / task.video_path).exists()
    assert task.video_filename == "gate.mp4"
    assert result.artifact_path is None
    assert result.purged_at is not None

@pytest.mark.asyncio
async def test_dry_run_deletes_nothing(session, tmp_path):
    task, _ = await _expired_task(session, tmp_path)
    stats = await VisionCleanupService(session, str(tmp_path)).run(days=90, dry_run=True)
    assert stats.deleted_files == 0
    assert Path(tmp_path / task.video_path).exists()

@pytest.mark.asyncio
async def test_purged_task_results_returns_404(client, admin_token, purged_task):
    r = await client.get(f"/api/v1/vision/tasks/{purged_task.task_uid}/results",
                         headers=auth_headers(admin_token))
    assert r.status_code == 404
    assert r.json()["data"]["reason"] == "artifact_missing"
```

- [ ] **Step 2：实现脚本**

脚本解析参数、构造 async session、按 `created_at < now - days` 查任务、逐个删除并提交；`--dry-run` 只统计；`--purge-db` 缺 `--confirm` 时以退出码 2 拒绝；退出码 0/1；异常回滚并打印 task_uid；审计必须写 `actor`。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_cleanup.py -q`
Expected: 默认保留汇总、dry-run 不删、purge 后 404。

---

## Phase P4：前端

### Task 20：API、Store、纯函数

**Files:**
- Create: `web/src/api/vision.ts`
- Create: `web/src/stores/vision.ts`
- Create: `web/src/utils/vision.ts`
- Test: `web/tests/test_vision_utils.spec.ts`

**Interfaces:**
- `visionApi`：`health/overview/models/tasks/createTask/task/results/cancel/retry/trigger/events/analyze/analyzeEvent`。
- `useVisionStore`：`task`、`frames`、`events`、`loading`、`error`、`polling`；`startPolling(uid)`、`stopPolling()`、`loadTask(uid)`。
- `findFrameAt(frames, timeMs)`：最后一个 `timestamp_ms <= timeMs` 的帧；若都大于则返回第一帧。
- `fitVideoRect(videoW, videoH, displayW, displayH)`：按 `object-fit: contain` 计算 `scale`、`offsetX`、`offsetY`，处理黑边。
- `scaleBBox(bbox, videoW, videoH, displayW, displayH)`：返回 display 坐标系（含黑边偏移）的 `{x1,y1,x2,y2}`。
- `filterDetections(frame, minConfidence, className)`。

- [ ] **Step 1：写纯函数测试**

```ts
import { describe, expect, it } from 'vitest'
import { findFrameAt, scaleBBox, filterDetections } from '@/utils/vision'

it('keeps current frame until next sampled frame', () => {
  const frames = [{ frame_id: 0, timestamp_ms: 0 }, { frame_id: 5, timestamp_ms: 200 }]
  expect(findFrameAt(frames, 199)?.frame_id).toBe(0)
  expect(findFrameAt(frames, 200)?.frame_id).toBe(5)
})

it('scales bbox with letterboxed video', () => {
  // 100x100 视频放进 200x100 容器：contain 后左右各 50px 黑边
  expect(scaleBBox({ x1: 10, y1: 20, x2: 30, y2: 40 }, 100, 100, 200, 100))
    .toEqual({ x1: 60, y1: 20, x2: 80, y2: 40 })
})

it('scales bbox with horizontal letterbox', () => {
  // 640x480 视频放进 640x360 容器：contain 后实际画布 480x360，左右各 80px 黑边
  expect(scaleBBox({ x1: 0, y1: 0, x2: 640, y2: 480 }, 640, 480, 640, 360))
    .toEqual({ x1: 80, y1: 0, x2: 560, y2: 360 })
})
```

- [ ] **Step 2：实现 store 轮询**

```ts
let timer: ReturnType<typeof setTimeout> | null = null
let delay = 2000

async function poll(uid: string) {
  try {
    const data = await visionApi.task(uid)
    task.value = data
    delay = 2000
    if (['completed', 'failed', 'cancelled'].includes(data.status)) {
      stopPolling()
      if (data.status === 'completed') await loadResults(uid)
      return
    }
  } catch {
    delay = Math.min(delay * 2, 30000)   // 网络异常指数退避，上限 30s
  }
  timer = setTimeout(() => poll(uid), delay)
}
```

- [ ] **Step 3：运行前端单元测试**

Run: `cd web && npm test -- test_vision_utils.spec.ts`
Expected: 通过。

### Task 21：路由、导航与五个页面骨架

**Files:**
- Modify: `web/src/router/index.ts`
- Modify: `web/src/layouts/AppLayout.vue`
- Create: `web/src/views/vision/VisionLayout.vue`
- Create: `web/src/views/vision/Overview.vue`
- Create: `web/src/views/vision/Detect.vue`
- Create: `web/src/views/vision/Records.vue`
- Create: `web/src/views/vision/TaskDetail.vue`
- Create: `web/src/views/vision/Models.vue`
- Test: `web/tests/test_vision_pages.spec.ts`

**Interfaces:**
- 路由 `/vision`（name `vision`，`meta.nav=true`，icon `vision`）→ 子路由：`''` Overview、`detect`、`records`、`records/:taskUid`（nav=false，activeMenu=/vision/records）、`models`。
- `AppLayout.iconMap` 增加 `vision: Camera`。
- 所有页面用 `AsyncState`；上传/取消/重试/研判按 `auth.role` 显示。

- [ ] **Step 1：写页面测试**

```ts
it('records page renders empty state', async () => {
  const wrapper = mount(Records, { global: { plugins: [router, pinia], stubs: ['AsyncState'] } })
  expect(wrapper.text()).toContain('暂无识别记录')
})

it('viewer does not see upload button', async () => {
  auth.role = 'viewer'
  const wrapper = mount(Detect)
  expect(wrapper.find('[data-test="upload"]').exists()).toBe(false)
})
```

- [ ] **Step 2：实现路由与页面**

使用 `observability` 分组相同的嵌套路由结构；`VisionLayout` 只放 `<router-view />` 与标题；列表页使用 `el-table`、状态筛选、300ms 防抖搜索、分页 20。

- [ ] **Step 3：运行 vue-tsc 与 vitest**

Run: `cd web && npm test -- test_vision_pages.spec.ts && npx vue-tsc --noEmit`
Expected: 通过。

### Task 22：VisionPlayer、DetectionPanel 与识别页

**Files:**
- Create: `web/src/components/vision/VisionPlayer.vue`
- Create: `web/src/components/vision/DetectionPanel.vue`
- Modify: `web/src/views/vision/Detect.vue`
- Test: `web/tests/test_vision_player.spec.ts`

**Interfaces:**
- `VisionPlayer` props：`src`、`frames`、`videoWidth`、`videoHeight`、`selectedId`；事件 `select`。
- Canvas 与 `<video>` 同尺寸；`<video>` 使用 `object-fit: contain`，Canvas 覆盖整个播放区域并按 `fitVideoRect` 计算黑边偏移。
- `requestAnimationFrame` + `timeupdate` + `seeked` + `loadedmetadata` 触发重绘；`resize` 时重算缩放。
- 当前帧 = `findFrameAt(frames, video.currentTime * 1000)`。
- 视频加载失败显示明确错误，不绘制任何框。
- `DetectionPanel` 显示当前帧编号/时间/推理耗时、按置信度排序的目标、点击 seek。

- [ ] **Step 1：写 Canvas 绘制测试**

```ts
it('draws scaled boxes for current time', async () => {
  const ctx = { clearRect: vi.fn(), strokeRect: vi.fn(), fillText: vi.fn(), fillRect: vi.fn() }
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ctx)
  const wrapper = mount(VisionPlayer, {
    props: { src: '/v.mp4', frames: [{ frame_id: 0, timestamp_ms: 0, detections: [{
      detection_id: '0-0', class_name: 'person', confidence: 0.9,
      bbox: { x1: 10, y1: 10, x2: 50, y2: 90 },
    }]}], videoWidth: 640, videoHeight: 480 },
  })
  await wrapper.find('video').trigger('loadedmetadata')
  expect(ctx.strokeRect).toHaveBeenCalled()
  expect(ctx.fillText).toHaveBeenCalledWith(expect.stringContaining('person'), expect.any(Number), expect.any(Number))
})
```

- [ ] **Step 2：实现播放器**

```ts
function draw() {
  const canvas = canvasRef.value
  const video = videoRef.value
  if (!canvas || !video) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  canvas.width = video.clientWidth
  canvas.height = video.clientHeight
  ctx.clearRect(0, 0, canvas.width, canvas.height)
  const frame = findFrameAt(props.frames, video.currentTime * 1000)
  const videoW = props.videoWidth || video.videoWidth || 1
  const videoH = props.videoHeight || video.videoHeight || 1
  const { scale, offsetX, offsetY } = fitVideoRect(
    videoW, videoH, video.clientWidth, video.clientHeight,
  )
  for (const d of frame?.detections ?? []) {
    const x = offsetX + d.bbox.x1 * scale, y = offsetY + d.bbox.y1 * scale
    const w = (d.bbox.x2 - d.bbox.x1) * scale, h = (d.bbox.y2 - d.bbox.y1) * scale
    ctx.strokeStyle = d.detection_id === props.selectedId ? '#f56c6c' : '#409eff'
    ctx.lineWidth = d.detection_id === props.selectedId ? 3 : 2
    ctx.strokeRect(x, y, w, h)
    ctx.fillStyle = 'rgba(0,0,0,.65)'
    ctx.fillRect(x, Math.max(0, y - 18), 110, 18)
    ctx.fillStyle = '#fff'
    ctx.fillText(`${d.class_name} ${(d.confidence * 100).toFixed(1)}%`, x + 4, Math.max(12, y - 4))
  }
}
```

`Detect.vue` 上传表单：后缀/200MB 客户端校验；参数：置信度 0.05–0.95、抽帧 `自动/1/5/10/25`（自动传 `target_samples=900`）、区域 JSON 折叠框；提交成功后 `store.startPolling(uid)`。

- [ ] **Step 3：运行测试**

Run: `cd web && npm test -- test_vision_player.spec.ts && npx vue-tsc --noEmit`
Expected: 通过。

### Task 23：识别记录、任务详情、模型页、总览页

**Files:**
- Modify: `web/src/views/vision/Records.vue`
- Modify: `web/src/views/vision/TaskDetail.vue`
- Modify: `web/src/views/vision/Models.vue`
- Modify: `web/src/views/vision/Overview.vue`

- [ ] **Step 1：实现记录页**

表格列：文件名、状态、进度、时长、处理帧/检测数、事件数、模型版本、创建时间；筛选状态 + 文件名防抖；分页 20；空态区分“无记录/筛选无结果”；行点击进详情。

- [ ] **Step 2：实现详情页**

视频播放器 + 汇总卡 + 当前帧目标列表 + 事件列表 + 工作流 Run/Trace 链接 + Agent 研判结果 + 取消/重试/研判按钮；`artifact_missing` 时显示“结果文件已清理”，不显示假框。

- [ ] **Step 3：实现模型页与总览页**

模型页常驻 `el-alert`：AGPL-3.0、两条授权路径、不默认商用；显示 DB 模型行 + 实时 health；不可达显示启动命令提示。总览 4 张统计卡 + 最近 5 个任务 + 服务状态卡。

- [ ] **Step 4：运行前端全量**

Run: `cd web && npm test && npx vue-tsc --noEmit`
Expected: 全部通过。

---

## Phase P5 联动验收（已由 Task 17/18 实现，本阶段只验证）

### Task 24：联动集成验收

**Files:**
- Test: `backend/tests/integration/test_vision_linkage.py`

- [ ] **Step 1：写端到端测试**

```python
@pytest.mark.asyncio
async def test_complete_task_triggers_one_run_and_one_agent(session, seeded_workflow, vision_agent):
    # _complete_task 必须走真实 ingest：结果 → 事件 → completed → 自动触发工作流
    task, result = await _complete_task(session, with_event=True)
    assert result.trigger_status == "triggered"
    await VisionTriggerService(session).trigger(task, result)   # 重放幂等
    await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    await VisionAgentService(session).analyze_task(task.task_uid, user=admin)
    agent_run = await _agent_run_by_id(session, result.agent_run_id)  # 测试辅助，直接查 agent_runs
    assert len(await _runs_for(task.task_uid)) == 1
    assert await _agent_run_count(task) == 1
    assert result.workflow_run_id is not None
    assert result.agent_run_id is not None
    assert result.agent_event_id is not None
    assert result.agent_trace_id == agent_run.trace_id
```

- [ ] **Step 2：验证关联字段**

断言 `vision_tasks.task_uid`、`vision_events.payload.task_uid`、`workflow_runs.trigger_payload.vision_task_uid`、`vision_results.task_uid/agent_event_id/workflow_run_id/agent_run_id/agent_trace_id`、`agent_runs.trace_id` 能串成一条链。

- [ ] **Step 3：运行测试**

Run: `cd backend && python -m pytest tests/integration/test_vision_linkage.py -q`
Expected: 通过；重复调用不产生第二条 Run / 第二个 AgentRun。

---

## Phase P6：Docker、文档与验收

### Task 25：Docker 镜像与 compose profile

**Files:**
- Create: `docker/vision.Dockerfile`
- Modify: `docker/compose/docker-compose.yml`
- Test: `docker compose config`

- [ ] **Step 1：写 Dockerfile**

```dockerfile
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY vision-service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir -r /app/requirements.txt
COPY vision-service/app /app/app
COPY vision-service/scripts /app/scripts
ENV VISION_MODEL_PATH=/app/weights/yolo11n.pt VISION_JOBS_DIR=/data/vision/jobs
EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=20 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8001/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
```

- [ ] **Step 2：改 compose**

新增 `vision` 服务，`profiles: ["vision"]`、`vision_data:/data/vision`、`vision_weights:/app/weights`，**不写 `depends_on`**，也不被 `api` 的 `depends_on` 引用；声明两个 volume。

`api` 增加以下环境变量与卷：

```yaml
environment:
  VISION_ENABLED: "true"
  VISION_SERVICE_URL: http://vision:8001
  VISION_SERVICE_TOKEN: ${VISION_SERVICE_TOKEN:-}
  VISION_STORAGE_DIR: /data/vision
  VISION_TRIGGER_ENABLED: "true"
volumes:
  - vision_data:/data/vision
```

`vision` 服务必须显式限定可读目录，避免接受任意宿主机绝对路径：

```yaml
vision:
  image: edgepilot-vision:local
  build:
    context: ../..
    dockerfile: docker/vision.Dockerfile
  profiles: ["vision"]
  environment:
    VISION_JOBS_DIR: /data/vision/jobs
    VISION_MEDIA_ROOTS: /data/vision
    VISION_MODEL_PATH: /app/weights/yolo11n.pt
    VISION_SERVICE_TOKEN: ${VISION_SERVICE_TOKEN:-}
  volumes:
    - vision_data:/data/vision
    - vision_weights:/app/weights
  ports:
    - "8001:8001"
```

- [ ] **Step 3：获取权重（首次启动前）**

镜像不内置权重；空权重时 `/health` 为 `degraded`，任务明确失败为 `model_unavailable`，不得伪造推理结果。

```bash
cd docker/compose
# 联网环境：容器内下载并打印 SHA-256
docker compose -f docker-compose.yml --profile vision run --rm vision python scripts/fetch_model.py
# 离线环境：把已核验的权重复制进命名卷
docker compose -f docker-compose.yml --profile vision up -d vision
docker compose -f docker-compose.yml cp ../../vision-service/weights/yolo11n.pt vision:/app/weights/yolo11n.pt
```

- [ ] **Step 4：验证既有启动方式不变**

Run:
```bash
cd docker/compose
docker compose -f docker-compose.yml --profile app config --services
```
Expected: 输出不含 `vision`；再加 `--profile vision` 时包含 `vision`。

- [ ] **Step 5：验证共享卷权限与双向读写**

Run:
```bash
docker compose -f docker-compose.yml --profile app --profile vision up -d
docker compose exec api sh -c 'test -w /data/vision && touch /data/vision/.api-write && rm /data/vision/.api-write'
docker compose exec vision sh -c 'test -w /data/vision/jobs && touch /data/vision/.vision-write && rm /data/vision/.vision-write'
docker compose exec api sh -c 'ls -ld /data/vision /data/vision/jobs'
```
Expected: 两个容器都能在 `/data/vision` 创建/删除文件；目录属主一致；失败时记录实际 UID/GID 并在文档给出 `user:` 或 `chown` 修复建议，不得假设权限可用。

### Task 26：种子脚本、文档与许可

**Files:**
- Create: `backend/scripts/seed_vision_demo.py`
- Create: `backend/scripts/fetch_vision_sample.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`、`docs/api.md`、`docs/er.md`、`docs/runbook.md`、`docs/offline-install.md`、`docs/demo-script.md`、`docs/user-flows.md`
- Modify: `docs/specs/2026-11-26-edgepilot-vision-design.md`（许可与边界摘要，保持与 spec 一致）

- [ ] **Step 1：种子脚本**

`seed_vision_demo.py` 为默认租户写入 `vision_models` 的 `yolo11n` 行（幂等 upsert），并打印 AGPL 提示；不创建假任务、不写假结果。

- [ ] **Step 2：文档**

README 增加：模块定位、真实推理边界、启动命令、AGPL 两条路径。`docs/api.md` 增加 14 个接口。`docs/er.md` 增加 3 表。`docs/runbook.md` 增加服务重启/任务失败/权重缺失/磁盘清理。`docs/offline-install.md` 增加权重离线复制。`docs/demo-script.md` 增加真实视频演示步骤。`docs/user-flows.md` 增加上传→完成→播放→研判流程。`docs/specs/2026-11-26-edgepilot-vision-design.md` 与模型页（Task 23）必须同时写明 AGPL-3.0、遵守 AGPL 或购买 Ultralytics Enterprise License 两条路径、以及“不默认授予商业使用权限”。

- [ ] **Step 3：验证文档命令**

Run:
```bash
grep -n "AGPL-3.0\|Enterprise License\|yolo11n" README.md docs/er.md docs/runbook.md
```
Expected: 三处均命中；不得出现“默认可商用”。

### Task 27：全量测试与真实验收证据

**Files:**
- Test: `backend/tests/integration/test_vision_acceptance.py`
- Test: `backend/tests/integration/test_vision_resilience.py`
- Test: `web/tests/test_vision_pages.spec.ts`

- [ ] **Step 1：补齐验收测试**

必须覆盖：
1. 真实视频推理（`RUN_REAL_YOLO=1`）：至少 1 个 person，bbox 在界内，`simulated=false`。
2. 重复上传 + 并发上传：同文件同参数只有一个任务。
3. vision-service 重启：未完成任务 `failed(service_restarted)`；已完成任务仍可查。
4. 跨租户：任务/事件/结果/视频全部 404 或 403。
5. 签名 URL：1h 内 200；过期/篡改 403；`Range: bytes=0-1023` 返回 206，非法 Range 返回 416。
6. 事件重放：重复 ingest 不新增事件；重复触发不新增 Run。
7. vision 停止：登录、Agent 列表、工作流列表、事件列表、运行列表仍正常；`/vision/health` 返回 `reachable=false`。

- [ ] **Step 2：执行后端全量**

Run:
```bash
cd backend
python -m pytest tests/unit tests/integration -q
```
Expected: 全通过；记录通过数与耗时。

- [ ] **Step 3：执行真实推理**

Run:
```bash
cd vision-service
RUN_REAL_YOLO=1 VISION_MODEL_PATH=weights/yolo11n.pt python -m pytest tests/test_inference_real.py -v
```
Expected: 真实模型、真实视频通过；保存控制台输出与截图/录屏路径到交付报告。

- [ ] **Step 4：执行前端全量**

Run:
```bash
cd web
npm test
npx vue-tsc --noEmit
```
Expected: 全通过。

### Task 28：10 项交付清单

**Files:**
- Create: `docs/specs/2026-11-26-edgepilot-vision-delivery.md`

- [ ] **Step 1：回填实测数据**

文档必须包含（与用户要求的 10 项交付物一一对应）：
1. 新增/修改文件清单（与 git status 对齐）。
2. 架构与数据流（上传、轮询、事件、联动）。
3. 模型与版本（YOLO11n、权重 SHA-256、AGPL-3.0、两条授权路径）。
4. 新增 API 清单（14 个 backend + 8 个 vision-service）。
5. DB 变更（3 表 + 索引 + 工作流幂等唯一索引）。
6. 前端功能（5 页面、Canvas 坐标匹配、权限隐藏）。
7. 测试执行结果与通过率（真实数字，不用“预计”）。
8. 真实视频推理证据（命令、输出、截图/录屏路径、素材 SHA-256）。
9. Docker 启动方式（原有 `--profile app` 与新增 `--profile vision`）。
10. 已实现/未实现/后续规划（明确摄像头、跟踪、转码、GPU、多副本 advisory lock 未实现）。

- [ ] **Step 2：一致性检查**

Run:
```bash
git status --short
grep -rn "TODO\|TBD\|待补充" docs/specs/2026-11-26-edgepilot-vision-delivery.md
```
Expected: 无 TODO/TBD；文件清单与实际改动一致。

---

## 自审结果

**Spec 覆盖检查：**
- 上传/校验 → Task 9、11；任务生命周期 → Task 4、14；抽帧/PTS/上限 → Task 3；事件 → Task 13；工作流 → Task 17；Agent → Task 18；页面 → Task 21–23；数据模型 → Task 7；API → Task 16；状态机/恢复 → Task 4、14；签名/Range → Task 15；配置 → Task 8；安全/权限 → Task 16；日志 → Task 14、16；清理 → Task 19；Docker → Task 25；测试 → Task 6、24、27；交付 → Task 28。
- 用户补充的 10 条工程约束均有对应任务：轮询/恢复 Task 14；抽帧 Task 3；业务联动 Task 17/18；事件幂等 Task 13；上传幂等 Task 11；视频安全 Task 15；文件可靠性 Task 4/12；兼容性 Task 7/25；许可证 Task 1/26；验收 Task 27。

**占位符检查：** 本计划不含 TBD/TODO；每个代码步骤给出可执行代码或精确签名。**类型一致性检查：** `task_uid` 全程为字符串；`VisionTask.task_uid` 与 `vision_events.payload.task_uid`、`workflow_runs.trigger_payload.vision_task_uid` 同名；`agent_run_id/workflow_run_id/trace_id` 字段在结果表与响应中一致。

**已知边界（必须写入交付文档）：**
- 当前单 API 实例假设；多副本需要 PostgreSQL advisory lock（后续规划）。
- 不做摄像头/RTSP/WebRTC、目标跟踪、转码、HLS、对象存储、恶意文件扫描。
- 事件中心前端页面仍为占位；本模块只在 Vision 页面展示事件。
- 浏览器不支持 H.265 时只显示明确错误，不做转码。
