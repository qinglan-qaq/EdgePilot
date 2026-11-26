# 运行手册（Runbook）

面向本地开发、演示与单机离线部署。命令默认在仓库根目录执行；Git Bash 下
`make` 可能不可用，因此下面给出等价的 compose 命令。

```bash
export COMPOSE="docker compose -f docker/compose/docker-compose.yml --profile app"
```

## 1. 启动与停止

```bash
cp -n docker/compose/.env.example docker/compose/.env
# 修改 JWT_SECRET、POSTGRES_PASSWORD 等；真实密钥不要提交

$COMPOSE up -d postgres redis rabbitmq mosquitto
$COMPOSE run --rm api alembic upgrade head
$COMPOSE up -d --build
```

首次构建会编译 API 镜像并安装 Python 依赖；后续只改后端代码时也要
`--build`，因为 api/worker/scheduler 共用 `edgepilot-api:local` 镜像。

停止：

```bash
$COMPOSE down            # 保留数据卷
$COMPOSE down -v         # 删除数据卷（会清空数据库，谨慎）
```

## 2. 健康检查

```bash
$COMPOSE ps
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/v1/ping
```

`api` 容器健康检查访问 `/health`；`postgres/redis/rabbitmq` 各自有
`pg_isready`/`redis-cli ping`/`rabbitmq-diagnostics ping`。

日志：

```bash
$COMPOSE logs -f api
$COMPOSE logs -f worker
$COMPOSE logs -f scheduler
$COMPOSE logs --tail=200 postgres
```

## 3. 初始化演示数据

```bash
export SEED_PASSWORD='edgepilot-demo'   # 至少 8 位，自行替换

# 1) 账号：admin / operator / viewer
python backend/scripts/seed_users.py

# 2) 2 节点、4 相机、200 条模拟事件、3 篇知识文档
python backend/scripts/seed_demo.py

# 3) 演示 Agent/工作流与 Mock LLM 夹具；--run 会触发并审批一次运行
python backend/scripts/seed_workflow_demo.py --run
```

脚本幂等，可重复执行；模拟事件与截图均带 `simulated`/DEMO 标记，不冒充真实
检测结果。需要重置密码时：

```bash
SEED_PASSWORD='new-password' python backend/scripts/seed_users.py --reset-passwords
```

## 4. 数据库迁移

```bash
cd backend
python -m alembic current
python -m alembic upgrade head
```

容器内执行（离线包没有源码时使用）：

```bash
$COMPOSE run --rm api alembic upgrade head
$COMPOSE run --rm api alembic current
```

迁移失败先看 `alembic_version` 与目标版本；不要在业务运行中直接
`downgrade`，先停 worker/scheduler 并备份。

## 5. 前端

开发：

```bash
cd web
npm ci
npm run dev
# http://127.0.0.1:5173
```

生产静态产物：

```bash
npm run build          # 输出 web/dist
```

开发代理默认指向 `http://127.0.0.1:8000`，可用 `VITE_PROXY_TARGET` 覆盖。
Windows 上不要把代理写成 `localhost`，Node 可能解析到 IPv6 而 Docker 只监听
IPv4。

## 6. 可观测栈

```bash
docker compose -f docker/compose/docker-compose.yml \
  -f docker/compose/docker-compose.observability.yml \
  --profile app --profile observability up -d
```

- Prometheus：http://127.0.0.1:9090 ，抓取 `api:8000/metrics`
- Grafana：http://127.0.0.1:3000 ，账号 `admin`，密码由
  `GRAFANA_ADMIN_PASSWORD` 控制（默认仅用于本地，生产必须覆盖）
- 看板：`EdgePilot Overview`（请求速率、p95、审计写入、总请求数）

只在需要时启动；不启动时 api 的 `/metrics` 仍可访问。

## 7. 数据保留清理

```bash
# 只统计，不写库（默认）
$COMPOSE exec api python /app/scripts/retention.py --dry-run

# 确认后执行（不可逆）
$COMPOSE exec api python /app/scripts/retention.py --execute

# 单租户 / 自定义窗口
$COMPOSE exec api python /app/scripts/retention.py --dry-run \
  --tenant 00000000-0000-0000-0000-000000000001 \
  --trace-events-days 30 --events-days 30 --obs-days 90 --audit-days 365
```

- 默认：业务事件 90 天、观测 180 天、审计 365 天。
- `vision_events` 分批 1000 条删除，并清理 `storage_dir` 内截图；越界路径只
  告警，不删文件。
- 数据库错误直接失败（非零退出）；单文件错误只记 warning。
- 建议先 dry-run 核对行数，再在低峰执行；执行前备份。

## 8. 视觉识别（Vision Center）

### 8.1 启停

```bash
# 同时启动应用与推理服务（vision 是独立 profile）
docker compose -f docker/compose/docker-compose.yml \
  --profile app --profile vision up -d --build

# 只停/重启推理服务，不影响 api
docker compose -f docker/compose/docker-compose.yml --profile vision stop vision
docker compose -f docker/compose/docker-compose.yml --profile vision restart vision
```

- `vision` 只监听 `127.0.0.1:8001`；`api` 不依赖它，未启动时 `/vision/*`
  返回明确降级结果，其它模块照常可用。
- 视频/结果在卷 `vision_data`（`/data/vision`），权重在卷 `vision_weights`
  （`/app/weights`）；`down -v` 会同时删除这两类数据。

### 8.2 健康与模型

```bash
# 先登录拿 access_token
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@edgepilot.local","password":"edgepilot-demo"}' \
  | python -c "import json,sys;print(json.load(sys.stdin)['data']['access_token'])")

curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/vision/health
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/vision/models
curl -fsS http://127.0.0.1:8001/health
```

- `/vision/health`：`reachable=false` 时 HTTP 仍 200，`error` 写明不可达原因；
  模型字段优先补 DB 登记的许可事实。
- `/vision/models`：DB 登记 + 推理服务实时项；服务不可达时
  `live.status="unreachable"`，仍返回 DB 许可信息。
- 推理服务 `/health`：存储可写且模型 `ready` 才是 `ok`，否则 `degraded`。
- 模型许可固定为 **AGPL-3.0**：可遵守 AGPL 义务使用，或购买 Ultralytics
  **Enterprise License**；本仓库**不默认授予商业使用权限**。商用/对外服务前
  必须自行评估并落实授权。

### 8.3 任务卡住或失败

- `pending`：上传已落库并派发，后台轮询器每 `VISION_POLL_INTERVAL_S`（默认 2s）
  对账。长时间 `pending` 先看 api 日志、`VISION_SERVICE_URL` 与
  `GET /v1/jobs/{task_uid}`；派发失败的任务会保留视频供重试。
- `running` 超过 `VISION_TASK_TIMEOUT_S`（默认 1800s）会请求远端取消并落
  `failed(timeout)`。
- 错误码：`vision_service_unavailable`（连续 30 次不可达）、
  `vision_job_lost`（连续 3 次远端 404）、`service_restarted`（推理服务重启中断）、
  `model_unavailable`（权重未加载）、`artifact_*`、`purged`。
- 只有 `failed`/`cancelled` 可 `POST /vision/tasks/{uid}/retry`；重试派发同一任务
  与视频，不改 `dedup_key`、不新建任务，最多 `VISION_MAX_RETRIES`（默认 3）次。
- 取消是终态幂等：推理服务不可达时仍在平台侧置 `cancelled`，并在
  `error_message` 说明“推理服务不可达，任务已在平台侧取消”。

### 8.4 推理服务重启

- 作业状态持久化在 `/data/vision/jobs/<task_uid>/state.json`。重启时
  `pending/running` 的作业会被标记为 `failed(service_restarted)`，后端轮询器在
  下一次对账复制该终态；已完成任务的 DB 行、artifact 与视频都不受影响，仍可查询、
  播放与研判。
- 重启后不会自动重跑中断任务，需要用户在任务详情点“重试”。

### 8.5 权重缺失与 reload

- 权重不在镜像内。缺失时推理服务 `/health` 为 `degraded`，模型
  `load_status="missing_weights"`；`/vision/health` 仍 `reachable=true`，但新任务
  会失败并写 `model_unavailable`。已存在的结果不受影响。
- 首次或更新权重：

```bash
# 本机
cd vision-service && python scripts/fetch_model.py

# Docker：复制进运行中的容器后必须 reload，否则 /health 仍不 ready
docker compose -f docker/compose/docker-compose.yml cp \
  vision-service/weights/yolo11n.pt vision:/app/weights/yolo11n.pt
curl -X POST -H "X-Vision-Token: $VISION_SERVICE_TOKEN" \
  http://127.0.0.1:8001/v1/models/reload
```

`POST /v1/models/reload`（或重启容器）会重新计算权重 SHA-256 并加载模型；
权重缺失时不伪造 `model_version`，登记版本回退为 `yolo11n`。

### 8.6 磁盘清理

```bash
cd backend
# 先统计，不删文件（仍会为每个过期任务写一条审计）
python scripts/vision_cleanup.py --days 90 --actor ops@example.com --dry-run
# 确认后执行：删除视频与 jobs 目录，保留任务汇总与结果摘要
python scripts/vision_cleanup.py --days 90 --actor ops@example.com
# 彻底删除结果行（破坏性，必须同时给 --confirm）
python scripts/vision_cleanup.py --days 90 --actor ops@example.com --purge-db --confirm
```

- 默认保留天数来自 `VISION_RESULT_RETENTION_DAYS`（默认 90）。
- 默认策略：保留 `vision_tasks` 与 `vision_results` 汇总，仅清空
  `artifact_path` 并写 `purged_at`；`--purge-db` 才删除结果行并把任务标为
  `failed(purged)`。
- 每个任务写审计 `action="vision.cleanup"`。`vision_cleanup.py` 不在 API 镜像内，
  需在仓库 `backend` 目录（或挂载了源码的环境）执行，数据库用 `backend/.env`
  的 `DATABASE_URL`。

### 8.7 Token 与配置作用域

- `VISION_SERVICE_TOKEN` 同时用于后端请求头 `X-Vision-Token` 与推理服务校验；
  留空表示本地不校验（仅适合本机）。
- `docker/compose/.env` 与 `backend/.env` **互相独立**：compose 从自己的 `.env`
  插值并注入 api/vision 容器；本地直接启动 backend 时读 `backend/.env`。
  两种方式混用时，需要把同一 token 同时写进两处（或显式 export）再重启。
- 其它视觉开关（`VISION_ENABLED`、`VISION_TRIGGER_ENABLED`、
  `VISION_POLL_INTERVAL_S`、`VISION_MAX_UPLOAD_MB` 等）在后端配置侧；
  `VISION_MODEL_PATH`、`VISION_JOBS_DIR`、`VISION_MEDIA_ROOTS` 在推理服务侧。

## 9. 备份与恢复

```bash
$COMPOSE exec -T postgres pg_dump -U edgepilot -d edgepilot -Fc > backup.dump
# 恢复前确认目标库为空/已停写入
$COMPOSE exec -T postgres pg_restore -U edgepilot -d edgepilot --clean --if-exists < backup.dump
```

同时备份：`docker compose` 卷（截图/storage）、`docker/compose/.env`、
模型 `api_key_enc` 所需的 `SECRET_ENCRYPTION_KEY`。只备份数据库而丢失加密密钥，
`api_key_enc` 将无法解密。

## 10. 常见故障

| 现象 | 排查 |
|---|---|
| api 启动即退出 | `docker compose logs api`；确认迁移已执行、`DATABASE_URL` 指向 `postgres` 服务名 |
| 前端 502/代理失败 | 确认 api 8000 健康；检查 `VITE_PROXY_TARGET` 是否 `127.0.0.1` |
| 登录 401 | 是否执行过 `seed_users.py`；密码是否被重置 |
| 工作流一直 pending | worker 是否在线；rabbitmq 是否健康；看 worker 日志与 `runs/{id}/logs` |
| 审批后不继续 | scheduler 是否在线；节点 `heartbeat_at` 是否超时；检查 `workflow_node_runs` |
| 节点一直 offline | Mosquitto 1883 是否可达；edge-agent outbox 是否有积压；15s 心跳窗口 |
| 事件列表空 | 节点/设备是否已 seed；MQTT 上行是否通过（日志关键字 `event`） |
| Trace 为空 | `OBS_ENABLED=true`、采样率、写库失败日志；`/metrics` 的丢弃计数 |
| 成本显示不可用 | 模型配置缺少单价或 provider 未返回 usage；这是预期行为，不要手工补 0 |
| `/vision/health` reachable=false | `vision` profile 是否启动；`VISION_SERVICE_URL` 是否 `http://vision:8001`（容器）或 `http://127.0.0.1:8001`（本机） |
| `/vision/models` 只有 DB 行 | 推理服务不可达（`live.status=unreachable`）；先查推理服务 `/health` 与日志 |
| 视觉任务 pending 不前进 | 看 api 日志中的派发失败；确认推理服务队列未满（`/health` 的 `queue`）；确认视频路径在 `VISION_MEDIA_ROOTS` 内 |
| 视觉任务 failed(model_unavailable) | 权重缺失或未加载；复制权重后 `POST /v1/models/reload`，否则重试仍会失败 |
| 视觉服务重启后任务失败 | 未完成任务按 `failed(service_restarted)` 收口；在任务详情重试；已完成任务仍可查 |
| 视觉视频 403 | 签名 URL 过期（默认 1 小时）；重新打开任务详情取新 `video_url`，不要手工改 token |
| 视觉清理脚本找不到 | `vision_cleanup.py` 不在 API 镜像内；在仓库 `backend` 目录执行 |
| 容器内 retention 提示文件不存在 | 使用镜像内置路径 `/app/scripts/retention.py`；Git Bash 下容器绝对路径前加 `MSYS_NO_PATHCONV=1` |

## 11. 安全与权限要点

- 生产必须覆盖 `JWT_SECRET`、`POSTGRES_PASSWORD`、`GRAFANA_ADMIN_PASSWORD`；
  `SECRET_ENCRYPTION_KEY` 缺失时开发环境从 JWT 派生，生产必须显式提供。
- 默认采集模式 `REDACTED`；查看完整 prompt/工具参数需要 `FULL` 且写审计。
- 所有写操作按角色鉴权；跨租户访问返回 404。
- MQTT 默认匿名仅适合本地；生产应在 `mosquitto.conf` 启用认证并配置 TLS。
- 离线包导入后不要加 `--build`；镜像已随包提供，目标机通常没有构建上下文。

## 12. 验收命令

```bash
# 后端
cd backend && python -m pytest -q

# edge-agent（不依赖网络/数据库）
cd edge-agent && python -m pytest -q

# 前端
cd web && npm test && npm run build

# compose 配置
docker compose -f docker/compose/docker-compose.yml --profile app config -q
```

本仓库当前基线：后端 481、edge-agent 8、前端 84 个用例通过；三项交付指标
见 [eval.md](./eval.md)。
