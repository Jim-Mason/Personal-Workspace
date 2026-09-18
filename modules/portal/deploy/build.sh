#!/usr/bin/env bash
# ============================================================
# 开发工具箱 —— 一键构建脚本
# 在「构建机」上执行：装依赖 + 编译前端产物到 frontend/dist
# 之后把整个项目目录拷到跳板机即可，跳板机无需 Node 环境。
# ============================================================
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

log() { printf '\033[36m[build]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[build] 失败：%s\033[0m\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null 2>&1 || die "未找到 python3"
command -v npm >/dev/null 2>&1 || die "未找到 npm（构建前端需要 Node.js 18+）"

# ---------------- 后端依赖 ----------------
log "创建 Python 虚拟环境 .venv"
[ -d .venv ] || python3 -m venv .venv
log "安装后端依赖"
.venv/bin/python -m pip install --upgrade pip --quiet
.venv/bin/python -m pip install -r backend/requirements.txt --quiet
log "后端依赖就绪：$(.venv/bin/python --version)"

# ---------------- 前端产物 ----------------
log "安装前端依赖"
cd frontend
if [ -f package-lock.json ]; then
  npm ci --no-audit --no-fund
else
  npm install --no-audit --no-fund
fi

log "编译前端"
npm run build

cd "$APP_DIR"
[ -f frontend/dist/index.html ] || die "前端构建产物缺失：frontend/dist/index.html"

log "完成。下一步："
log "  1) cp .env.example .env 并填写 SECRET_KEY（openssl rand -hex 32）"
log "  2) 把整个目录同步到跳板机"
log "  3) 在跳板机执行 ./deploy/start.sh"
