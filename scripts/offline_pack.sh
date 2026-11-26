#!/usr/bin/env bash
# EdgePilot 离线部署包构建脚本（P7 Task 27）。
#
# 从仓库根目录运行：
#   bash scripts/offline_pack.sh                 # 构建 api 镜像 + 前端，打包核心镜像
#   bash scripts/offline_pack.sh --observability # 额外带上 Prometheus/Grafana 镜像与配置
#   bash scripts/offline_pack.sh --dry-run       # 只打印将要执行的步骤，不构建不打包
#   bash scripts/offline_pack.sh --load dist/offline/images.tar  # 在目标机导入镜像
#
# 产物（dist/offline/）：
#   images.tar          docker save 的核心镜像
#   web-dist.tar.gz     前端构建产物（web/dist）
#   docker/             compose 与可观测配置副本（保持相对挂载路径）
#   backend/demo/fixtures/llm/  compose 只读挂载的演示夹具
#   LOAD.md             离线导入与启动说明
#   manifest.json       git commit、时间、镜像 tag、文件大小
#   SHA256SUMS          全部产物校验和

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$REPO_ROOT/dist/offline"
API_IMAGE="edgepilot-api:local"

CORE_IMAGES=(
  "$API_IMAGE"
  "pgvector/pgvector:pg16"
  "redis:7-alpine"
  "eclipse-mosquitto:2"
  "rabbitmq:3.13-management-alpine"
)
OBS_IMAGES=(
  "prom/prometheus:v2.54.1"
  "grafana/grafana:11.3.0"
)

MODE="pack"
WITH_OBS=0
DRY_RUN=0
LOAD_TAR=""

usage() {
  cat <<'EOF'
用法：bash scripts/offline_pack.sh [选项]

选项：
  --observability     额外打包 Prometheus/Grafana 镜像与观测配置
  --dry-run           只做前置检查并打印计划，不构建、不 save
  --load <tar>        只执行 docker load -i <tar> 并打印后续 compose 步骤
  --output <dir>      输出目录（默认 dist/offline）
  -h, --help          显示本帮助

说明：
  - 默认构建 edgepilot-api:local 并执行 web 下的 npm run build；
  - 有 web/node_modules 时复用，否则执行 npm ci；
  - 失败一律非零退出并给出明确原因。
EOF
}

log() { printf '[offline_pack] %s\n' "$*"; }
fail() { printf '[offline_pack] ERROR: %s\n' "$*" >&2; exit 1; }

print_load_steps() {
  local bundle_dir="$1"
  cat <<EOF
[offline_pack] 镜像已导入。在离线机上按以下步骤启动：
  1. 进入离线包目录：cd ${bundle_dir}
  2. 复制环境变量模板：
     cp -n docker/compose/.env.example docker/compose/.env   # 按需修改，勿提交真实密钥
  3. 启动基础服务（postgres/redis/rabbitmq/mosquitto）：
     docker compose -f docker/compose/docker-compose.yml --profile app up -d \\
       postgres redis rabbitmq mosquitto
  4. 执行数据库迁移（不要加 --build，镜像已导入）：
     docker compose -f docker/compose/docker-compose.yml --profile app run --rm api \\
       alembic upgrade head
  5. 启动应用：
     docker compose -f docker/compose/docker-compose.yml --profile app up -d
  6. 可观测栈（仅当 manifest.json 的 observability_images 为 true 时）：
     docker compose -f docker/compose/docker-compose.yml \\
       -f docker/compose/docker-compose.observability.yml \\
       --profile app --profile observability up -d
  7. 前端静态产物：tar -xzf web-dist.tar.gz
  8. 数据保留（先 dry-run，再决定是否 execute）：
     docker compose -f docker/compose/docker-compose.yml --profile app exec api \\
       python /app/scripts/retention.py --dry-run
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --observability) WITH_OBS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --output)
      [[ $# -ge 2 ]] || fail "--output 需要一个目录参数"
      OUT_DIR="$2"; shift 2 ;;
    --load)
      [[ $# -ge 2 ]] || fail "--load 需要一个 tar 路径参数"
      MODE="load"; LOAD_TAR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "未知参数：$1（使用 --help 查看用法）" ;;
  esac
done

if [[ "$MODE" == "load" ]]; then
  [[ -n "$LOAD_TAR" ]] || fail "--load 需要 tar 路径"
  [[ -f "$LOAD_TAR" ]] || fail "镜像归档不存在：$LOAD_TAR"
  command -v docker >/dev/null 2>&1 || fail "未找到 docker"
  bundle_dir="$(cd -- "$(dirname -- "$LOAD_TAR")" && pwd)"
  if [[ -f "$bundle_dir/SHA256SUMS" ]]; then
    log "校验离线包完整性：$bundle_dir/SHA256SUMS"
    if command -v sha256sum >/dev/null 2>&1; then
      ( cd "$bundle_dir" && sha256sum -c SHA256SUMS ) || fail "离线包校验失败"
    elif command -v shasum >/dev/null 2>&1; then
      ( cd "$bundle_dir" && shasum -a 256 -c SHA256SUMS ) || fail "离线包校验失败"
    else
      log "未找到 sha256sum/shasum，跳过校验"
    fi
  fi
  log "docker load -i $LOAD_TAR"
  docker load -i "$LOAD_TAR" || fail "docker load 失败：$LOAD_TAR"
  print_load_steps "$bundle_dir"
  exit 0
fi

# ---------------------------------------------------------------- 前置检查
command -v docker >/dev/null 2>&1 || fail "未找到 docker"
command -v npm >/dev/null 2>&1 || fail "未找到 npm（需要 Node.js 构建前端）"
command -v tar >/dev/null 2>&1 || fail "未找到 tar"
[[ -n "$OUT_DIR" && "$OUT_DIR" != "/" && "$OUT_DIR" != "." && "$OUT_DIR" != "./" ]] \
  || fail "--output 不能是根目录或仓库根：$OUT_DIR"
[[ -f "$REPO_ROOT/docker/api.Dockerfile" ]] || fail "缺少 docker/api.Dockerfile"
[[ -f "$REPO_ROOT/docker/compose/docker-compose.yml" ]] || fail "缺少 docker/compose/docker-compose.yml"
[[ -f "$REPO_ROOT/web/package.json" ]] || fail "缺少 web/package.json"
[[ -f "$REPO_ROOT/scripts/retention.py" ]] || fail "缺少 scripts/retention.py"
[[ -f "$REPO_ROOT/backend/demo/fixtures/llm/demo_report.json" ]] \
  || fail "缺少 backend/demo/fixtures/llm（compose 只读挂载所需）"

IMAGES=("${CORE_IMAGES[@]}")
if [[ "$WITH_OBS" -eq 1 ]]; then
  IMAGES+=("${OBS_IMAGES[@]}")
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  log "mode=dry-run（不构建、不打包）"
  log "repo_root=$REPO_ROOT"
  log "output=$OUT_DIR"
  log "api_dockerfile=docker/api.Dockerfile"
  log "web_build=web (node_modules: $([[ -d "$REPO_ROOT/web/node_modules" ]] && echo present || echo absent -> npm ci))"
  log "images=${IMAGES[*]}"
  log "observability=$WITH_OBS"
  log "dry-run 前置检查通过"
  exit 0
fi

# ---------------------------------------------------------------- 构建镜像/前端
log "构建 $API_IMAGE（docker build -f docker/api.Dockerfile .）"
docker build -f "$REPO_ROOT/docker/api.Dockerfile" -t "$API_IMAGE" "$REPO_ROOT" \
  || fail "docker build 失败"

log "构建前端（web）"
if [[ -d "$REPO_ROOT/web/node_modules" ]]; then
  log "检测到 web/node_modules，复用现有依赖"
else
  log "未检测到 web/node_modules，执行 npm ci"
  ( cd "$REPO_ROOT/web" && npm ci ) || fail "npm ci 失败"
fi
( cd "$REPO_ROOT/web" && npm run build ) || fail "npm run build 失败"
[[ -d "$REPO_ROOT/web/dist" ]] || fail "前端构建产物缺失：web/dist"

# ---------------------------------------------------------------- 打包
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

# compose 里的挂载是相对 docker/compose/ 的 ../postgres、../mosquitto，
# 以及相对仓库根的 backend/demo/fixtures/llm；离线包按同样层级复制，
# 否则导入镜像后一启动就会因为宿主路径不存在而挂空目录。
log "复制 compose 与可观测配置 -> docker/"
cp -R "$REPO_ROOT/docker" "$OUT_DIR/docker"
mkdir -p "$OUT_DIR/backend/demo/fixtures"
cp -R "$REPO_ROOT/backend/demo/fixtures/llm" "$OUT_DIR/backend/demo/fixtures/"

for image in "${IMAGES[@]}"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    log "本地缺少 $image，执行 docker pull"
    docker pull "$image" || fail "docker pull 失败：$image"
  fi
done

log "docker save -> $OUT_DIR/images.tar（${#IMAGES[@]} 个镜像）"
docker save -o "$OUT_DIR/images.tar" "${IMAGES[@]}" || fail "docker save 失败"

log "打包前端产物 -> web-dist.tar.gz"
tar -czf "$OUT_DIR/web-dist.tar.gz" -C "$REPO_ROOT/web" dist || fail "前端产物打包失败"

cat > "$OUT_DIR/LOAD.md" <<'EOF'
# EdgePilot 离线包使用说明

本目录由 `scripts/offline_pack.sh` 生成，目录结构刻意保留仓库层级
（`docker/compose/` + `docker/observability/` + `backend/demo/fixtures/llm/`），
因为 compose 内的挂载路径是相对于这些位置的。所有文件均已写入 `SHA256SUMS`。

## 1. 校验并导入镜像

```bash
sha256sum -c SHA256SUMS          # macOS 用 shasum -a 256 -c SHA256SUMS
docker load -i images.tar
```

## 2. 准备环境变量

```bash
cp -n docker/compose/.env.example docker/compose/.env
# 按需修改 JWT_SECRET、GRAFANA_ADMIN_PASSWORD 等；不要把真实密钥提交到仓库
```

## 3. 启动基础服务并迁移

```bash
docker compose -f docker/compose/docker-compose.yml --profile app up -d \
  postgres redis rabbitmq mosquitto
docker compose -f docker/compose/docker-compose.yml --profile app run --rm api \
  alembic upgrade head
```

> 不要加 `--build`：镜像已随包导入，离线机没有构建上下文。
> 离线包没有业务源码，迁移必须通过镜像内的 alembic 执行。

## 4. 启动应用

```bash
docker compose -f docker/compose/docker-compose.yml --profile app up -d
```

## 5. 可观测栈（manifest.json 的 observability_images 为 true 时）

```bash
docker compose -f docker/compose/docker-compose.yml \
  -f docker/compose/docker-compose.observability.yml \
  --profile app --profile observability up -d
```

`observability_images=false` 表示本包未包含 Prometheus/Grafana 镜像，
overlay 文件虽然随包复制，但启动会因镜像缺失失败；联网机可用
`bash scripts/offline_pack.sh --observability` 重新打包。

Prometheus: http://localhost:9090 ，Grafana: http://localhost:3000 。
生产部署前务必通过 `GRAFANA_ADMIN_PASSWORD` 覆盖默认口令。

## 6. 前端静态产物

```bash
tar -xzf web-dist.tar.gz   # 解压出 dist/，交给 nginx 或现有静态服务器
```

## 7. 数据保留（先 dry-run）

```bash
docker compose -f docker/compose/docker-compose.yml --profile app exec api \
  python /app/scripts/retention.py --dry-run
# 确认统计无误后再执行（不可逆）：
docker compose -f docker/compose/docker-compose.yml --profile app exec api \
  python /app/scripts/retention.py --execute
```
EOF

# ---------------------------------------------------------------- 校验和与 manifest
file_size() {
  if stat -c%s "$1" >/dev/null 2>&1; then
    stat -c%s "$1"
  else
    wc -c < "$1" | tr -d ' '
  fi
}

manifest_file() {
  local label="$1"
  local path="$2"
  printf '    "%s": %s' "$label" "$(file_size "$path")"
}

# 键是包内相对路径（不是 basename）：compose 与模板按仓库层级存放。
MANIFEST_FILES=(
  "images.tar|$OUT_DIR/images.tar"
  "web-dist.tar.gz|$OUT_DIR/web-dist.tar.gz"
  "docker/compose/docker-compose.yml|$OUT_DIR/docker/compose/docker-compose.yml"
  "docker/compose/.env.example|$OUT_DIR/docker/compose/.env.example"
  "LOAD.md|$OUT_DIR/LOAD.md"
)
[[ -f "$OUT_DIR/docker/compose/docker-compose.observability.yml" ]] && \
  MANIFEST_FILES+=(
    "docker/compose/docker-compose.observability.yml|$OUT_DIR/docker/compose/docker-compose.observability.yml"
  )

GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
if [[ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)" ]]; then
  GIT_DIRTY="true"
else
  GIT_DIRTY="false"
fi
CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  printf '{\n'
  printf '  "created_at": "%s",\n' "$CREATED_AT"
  printf '  "git_commit": "%s",\n' "$GIT_COMMIT"
  printf '  "git_dirty": %s,\n' "$GIT_DIRTY"
  printf '  "observability_images": %s,\n' "$([[ "$WITH_OBS" -eq 1 ]] && echo true || echo false)"
  printf '  "images": ['
  for i in "${!IMAGES[@]}"; do
    [[ "$i" -gt 0 ]] && printf ', '
    printf '"%s"' "${IMAGES[$i]}"
  done
  printf '],\n'
  printf '  "files": {\n'
  for i in "${!MANIFEST_FILES[@]}"; do
    [[ "$i" -gt 0 ]] && printf ',\n'
    entry="${MANIFEST_FILES[$i]}"
    manifest_file "${entry%%|*}" "${entry#*|}"
  done
  printf '\n  }\n'
  printf '}\n'
} > "$OUT_DIR/manifest.json"

if command -v sha256sum >/dev/null 2>&1; then
  ( cd "$OUT_DIR" && find . -type f ! -name SHA256SUMS -print | LC_ALL=C sort \
      | while IFS= read -r f; do sha256sum "$f"; done > SHA256SUMS ) \
    || fail "生成 SHA256SUMS 失败"
else
  ( cd "$OUT_DIR" && find . -type f ! -name SHA256SUMS -print | LC_ALL=C sort \
      | while IFS= read -r f; do shasum -a 256 "$f"; done > SHA256SUMS ) \
    || fail "生成 SHA256SUMS 失败"
fi

log "完成：$OUT_DIR"
log "  images.tar    $(file_size "$OUT_DIR/images.tar") bytes"
log "  web-dist      $(file_size "$OUT_DIR/web-dist.tar.gz") bytes"
log "  manifest      $OUT_DIR/manifest.json"
log "  checksums     $OUT_DIR/SHA256SUMS"
