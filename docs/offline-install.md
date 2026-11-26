# 离线安装说明

离线包由 `scripts/offline_pack.sh` 在联网构建机生成，在目标机通过
`docker load` + `docker compose up` 启动，不需要源码、npm 或外网。

## 1. 生成离线包（联网机）

前置：Docker 可用、Node.js/npm 可用、演示夹具存在
（`backend/demo/fixtures/llm/demo_report.json`）。

```bash
# 只打包核心栈
bash scripts/offline_pack.sh

# 额外包含 Prometheus/Grafana 镜像与观测配置
bash scripts/offline_pack.sh --observability

# 只检查前置条件，不构建
bash scripts/offline_pack.sh --dry-run
```

默认产物在 `dist/offline/`：

```
dist/offline/
├── images.tar                # docker save 的镜像
├── web-dist.tar.gz           # 前端静态产物（web/dist）
├── docker/                   # compose、Dockerfile、postgres/mosquitto/observability 配置
├── backend/demo/fixtures/llm/# compose 只读挂载的 Mock LLM 夹具
├── LOAD.md                   # 目标机操作步骤
├── manifest.json             # git commit、时间、镜像 tag、文件大小
└── SHA256SUMS                # 全部文件校验和
```

关键点：

- 目录保持仓库相对层级，因为 compose 中的挂载是
  `../postgres/init`、`../mosquitto/mosquitto.conf`、`../observability`、
  `../../backend/demo/fixtures/llm`。
- `manifest.json` 的 `observability_images` 表示是否包含 Prometheus/Grafana；
  为 `false` 时 overlay 文件仍在，但启动观测栈会因镜像缺失失败。
- 离线包不包含业务源码；数据库迁移通过镜像内的 alembic 执行。
- 目标机架构必须与构建机一致（当前镜像为 linux/amd64）。
- **视觉推理镜像** `edgepilot-vision:local`（CPU torch）目前不在
  `offline_pack.sh` 的 `CORE_IMAGES` 内。需要视觉能力时，在联网构建机先
  `docker compose -f docker/compose/docker-compose.yml --profile vision build vision`，
  再单独保存并随包分发：

  ```bash
  docker save -o dist/offline/vision-image.tar edgepilot-vision:local
  # 目标机
  docker load -i vision-image.tar
  ```

  权重 `yolo11n.pt` 不打进镜像；在构建机用
  `vision-service/scripts/fetch_model.py` 下载后，把文件随离线包携带（不要提交
  git）。

## 2. 目标机校验与导入

```bash
cd offline
sha256sum -c SHA256SUMS          # macOS: shasum -a 256 -c SHA256SUMS
docker load -i images.tar
```

也可以使用脚本自带的加载路径（会先校验再 load）：

```bash
bash scripts/offline_pack.sh --load /path/to/offline/images.tar
```

确认镜像：

```bash
docker images | grep -E 'edgepilot|pgvector|redis|mosquitto|rabbitmq|prometheus|grafana'
```

## 3. 配置环境变量

```bash
cp -n docker/compose/.env.example docker/compose/.env
```

至少覆盖：

| 变量 | 说明 |
|---|---|
| `POSTGRES_PASSWORD` | 数据库口令 |
| `JWT_SECRET` | 登录 JWT 签名密钥 |
| `SECRET_ENCRYPTION_KEY` | Fernet 密钥，用于解密 `api_key_enc`；丢失则已有模型密钥不可用 |
| `GRAFANA_ADMIN_PASSWORD` | 仅观测 profile |
| `LLM_MODE` | 离线默认 `mock`，不要填真实密钥到仓库 |

`LLM_FIXTURE_DIR` 已在 compose 中固定为 `demo/fixtures/llm`，离线包已带夹具。

## 4. 启动基础服务并迁移

```bash
docker compose -f docker/compose/docker-compose.yml --profile app up -d \
  postgres redis rabbitmq mosquitto

docker compose -f docker/compose/docker-compose.yml --profile app run --rm api \
  alembic upgrade head
```

> 不要加 `--build`。离线机没有构建上下文，镜像已导入。

检查迁移：

```bash
docker compose -f docker/compose/docker-compose.yml --profile app run --rm api \
  alembic current
```

## 5. 启动应用

```bash
docker compose -f docker/compose/docker-compose.yml --profile app up -d
docker compose -f docker/compose/docker-compose.yml --profile app ps
curl -fsS http://127.0.0.1:8000/health
```

## 6. 前端

```bash
tar -xzf web-dist.tar.gz     # 解出 dist/
```

将 `dist/` 交给 nginx 或现有静态服务器；反向代理把 `/api` 转发到
`http://api:8000`（或宿主 `127.0.0.1:8000`）。SPA 需要 fallback 到
`index.html`。

## 7. 视觉识别（可选 `vision` profile）

前置：`edgepilot-vision:local` 已 `docker load`（见 §1），且权重文件
`yolo11n.pt` 已随离线包携带。

```bash
docker compose -f docker/compose/docker-compose.yml \
  --profile app --profile vision up -d
```

**离线复制权重（推荐）**：把权重复制进运行中的容器，然后必须 reload：

```bash
docker compose -f docker/compose/docker-compose.yml cp \
  /path/to/yolo11n.pt vision:/app/weights/yolo11n.pt
docker compose -f docker/compose/docker-compose.yml exec vision \
  ls -l /app/weights/yolo11n.pt
curl -X POST -H "X-Vision-Token: $VISION_SERVICE_TOKEN" \
  http://127.0.0.1:8001/v1/models/reload
```

顺序固定为 **copy → reload → verify**：只复制不 reload 时，`/health` 仍报
`degraded`/`load_error`，新任务会失败并写 `model_unavailable`。

**有网环境也可让容器自行下载**（无需 `docker compose cp`）：

```bash
docker compose -f docker/compose/docker-compose.yml exec vision \
  python scripts/fetch_model.py
curl -X POST -H "X-Vision-Token: $VISION_SERVICE_TOKEN" \
  http://127.0.0.1:8001/v1/models/reload
```

验证：

```bash
curl -fsS http://127.0.0.1:8001/health
# 期望 status=ok、model.load_status=ready、storage.writable=true

curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/vision/health
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/api/v1/vision/models
```

`/health` 返回 200 但 `status=degraded` 表示权重未加载；`/vision/health` 的
`reachable=false` 表示后端没连上推理服务。视频、artifact 与作业状态都在
`vision_data` 卷，权重在 `vision_weights` 卷；升级/回滚前一并备份。

## 8. 可观测栈（可选）

仅当 `manifest.json` 的 `observability_images=true`：

```bash
docker compose -f docker/compose/docker-compose.yml \
  -f docker/compose/docker-compose.observability.yml \
  --profile app --profile observability up -d
```

- Prometheus http://目标机:9090 ，Grafana http://目标机:3000
- Grafana 首次登录 `admin` / `GRAFANA_ADMIN_PASSWORD`
- 看板 `EdgePilot Overview` 自动加载

## 9. 初始化演示数据（可选）

离线包没有 seed 脚本；需要演示账号/数据时，从仓库另行拷贝
`backend/scripts/` 与 Python 环境，或提前在构建机执行后备份数据库。若目标机
已有数据库备份，直接 `pg_restore` 即可：

```bash
docker compose -f docker/compose/docker-compose.yml exec -T postgres \
  pg_restore -U edgepilot -d edgepilot --clean --if-exists < backup.dump
```

## 10. 数据保留

镜像内置 `/app/scripts/retention.py`：

```bash
docker compose -f docker/compose/docker-compose.yml --profile app exec api \
  python /app/scripts/retention.py --dry-run

# 确认统计后执行
docker compose -f docker/compose/docker-compose.yml --profile app exec api \
  python /app/scripts/retention.py --execute
```

Git Bash 下如果路径被 MSYS 改写，命令前加 `MSYS_NO_PATHCONV=1`。

## 11. 验收清单

- [ ] `sha256sum -c SHA256SUMS` 全部 OK
- [ ] `docker load` 成功，镜像 tag 与 `manifest.json` 一致
- [ ] `alembic current` 到达 `0006_vision`
- [ ] `/health` 返回 200，`/metrics` 返回 Prometheus 文本
- [ ] 前端能登录，Dashboard 有计数
- [ ] 触发一次工作流运行，Trace Detail 可见 span 树
- [ ] 观测 profile 下 Prometheus target UP，Grafana 看板有数据
- [ ] retention dry-run 行数与预期一致
- [ ] （可选 vision）`http://127.0.0.1:8001/health` 为 `ok`、`model.load_status=ready`
- [ ] （可选 vision）`GET /api/v1/vision/health` 的 `reachable=true`，上传一次真实视频可完成并播放目标框

## 12. 升级与回滚

升级：

1. 构建机生成新离线包（新 `images.tar`）。
2. 目标机先备份数据库。
3. `docker load` 新镜像，执行 `alembic upgrade head`。
4. 重建 api/worker/scheduler 容器（使用新镜像 tag；本仓库固定
   `edgepilot-api:local`，需先 `docker compose down` 再 `up -d`）。

回滚：

1. 保留上一版 `images.tar` 与数据库备份。
2. `docker load` 旧镜像，恢复数据库备份。
3. 不要对已升级的库直接 `alembic downgrade` 跨多个版本；优先恢复备份。

## 13. 常见问题

| 现象 | 处理 |
|---|---|
| `docker save` 报缺少镜像 | `scripts/offline_pack.sh` 会先检查并 `docker pull`；目标机无外网时必须在构建机补齐 |
| compose 启动挂载为空 | 不要只拷贝 compose 文件；整个 `docker/` 目录与 `backend/demo/fixtures/llm` 必须在包内保持层级 |
| 迁移报 `relation does not exist` | 先启动 postgres 并健康，再跑 `run --rm api alembic upgrade head` |
| 前端 404/刷新丢失 | 静态服务器未配置 SPA fallback |
| Grafana 无数据 | `observability_images=false` 或 Prometheus 抓取目标不是 `api:8000` |
| 成本全是“不可用” | 离线环境没有真实 LLM 用量/单价；这是预期，不估算 |
