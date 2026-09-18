#!/usr/bin/env bash
# ============================================================
# 开发工具箱 —— 启动脚本（跳板机 / Linux）
#
# 前置：已执行 deploy/build.sh（存在 .venv 与 frontend/dist），
#       且已按 .env.example 配置好 .env
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR/backend"

log() { printf '\033[36m[start]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[start] 失败：%s\033[0m\n' "$*" >&2; exit 1; }

[ -x "$APP_DIR/.venv/bin/python" ] || die "找不到 .venv/bin/python，请先执行 ./deploy/build.sh"
[ -f "$APP_DIR/.env" ] || die "找不到 .env，请先 cp .env.example .env 并填写 SECRET_KEY"
[ -f "$APP_DIR/frontend/dist/index.html" ] || die "找不到前端产物，请先执行 ./deploy/build.sh"

# 从 .env 读端口，供 --port 使用（不 export，避免覆盖应用自身读取的配置）
PORT_VALUE="$(grep -E '^PORT=' "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "' || true)"
HOST_VALUE="$(grep -E '^HOST=' "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' "' || true)"
PORT_VALUE="${PORT_VALUE:-8000}"
HOST_VALUE="${HOST_VALUE:-0.0.0.0}"

# --workers 固定为 1：
#   1) SQLite 的写并发有限；
#   2) 启动时的迁移与初始管理员创建在 lifespan 里执行，
#      多 worker 会并发跑同一份迁移，存在竞争风险。
#   内网门户量级下，单进程 asyncio 完全够用（代理转发是 IO 密集）。
log "数据库迁移"
"$APP_DIR/.venv/bin/python" -m app.db.migrate

log "启动服务 ${HOST_VALUE}:${PORT_VALUE}"
exec "$APP_DIR/.venv/bin/python" -m uvicorn app.main:app \
  --host "$HOST_VALUE" \
  --port "$PORT_VALUE" \
  --workers 1 \
  --proxy-headers \
  --forwarded-allow-ips '*'
