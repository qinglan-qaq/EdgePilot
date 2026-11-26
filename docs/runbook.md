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

## 8. 备份与恢复

```bash
$COMPOSE exec -T postgres pg_dump -U edgepilot -d edgepilot -Fc > backup.dump
# 恢复前确认目标库为空/已停写入
$COMPOSE exec -T postgres pg_restore -U edgepilot -d edgepilot --clean --if-exists < backup.dump
```

同时备份：`docker compose` 卷（截图/storage）、`docker/compose/.env`、
模型 `api_key_enc` 所需的 `SECRET_ENCRYPTION_KEY`。只备份数据库而丢失加密密钥，
`api_key_enc` 将无法解密。

## 9. 常见故障

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
| 容器内 retention 提示文件不存在 | 使用镜像内置路径 `/app/scripts/retention.py`；Git Bash 下容器绝对路径前加 `MSYS_NO_PATHCONV=1` |

## 10. 安全与权限要点

- 生产必须覆盖 `JWT_SECRET`、`POSTGRES_PASSWORD`、`GRAFANA_ADMIN_PASSWORD`；
  `SECRET_ENCRYPTION_KEY` 缺失时开发环境从 JWT 派生，生产必须显式提供。
- 默认采集模式 `REDACTED`；查看完整 prompt/工具参数需要 `FULL` 且写审计。
- 所有写操作按角色鉴权；跨租户访问返回 404。
- MQTT 默认匿名仅适合本地；生产应在 `mosquitto.conf` 启用认证并配置 TLS。
- 离线包导入后不要加 `--build`；镜像已随包提供，目标机通常没有构建上下文。

## 11. 验收命令

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
