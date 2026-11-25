#!/usr/bin/env bash
# 等待本地依赖服务端口可连接（PG 5433 / Redis 6379 / MQTT 1883 / RabbitMQ 5672）。
# 最多等待 90 秒；超时输出仍未就绪的端口并以非 0 退出。
set -euo pipefail

PYTHON_BIN="${PYTHON:-python}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "未找到 Python 解释器：$PYTHON_BIN（可通过 PYTHON 环境变量指定）" >&2
  exit 1
fi

exec "$PYTHON_BIN" - 5433 6379 1883 5672 <<'PY'
import socket
import sys
import time

ports = [int(p) for p in sys.argv[1:]]
deadline = time.time() + 90
pending = ports

while pending and time.time() < deadline:
    still_pending = []
    for port in pending:
        try:
            with socket.create_connection(("localhost", port), timeout=2):
                pass
        except OSError:
            still_pending.append(port)
    pending = still_pending
    if pending:
        time.sleep(2)

if pending:
    missing = ", ".join(str(p) for p in pending)
    print(f"等待超时（90s），以下端口仍不可连接：{missing}", file=sys.stderr)
    sys.exit(1)

ready = ", ".join(str(p) for p in ports)
print(f"依赖服务端口已就绪：{ready}")
PY
