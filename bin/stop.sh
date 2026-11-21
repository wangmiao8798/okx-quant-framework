#!/bin/bash
#
# bin/stop.sh — 优雅停止 OKX 量化交易系统
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

PID_FILE="${PROJECT_DIR}/data/run.pid"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

if [ ! -f "${PID_FILE}" ]; then
    warn "PID 文件不存在: ${PID_FILE}"
    warn "尝试通过 pkill 停止..."
    pkill -f "main.py" 2>/dev/null && info "已停止" || error "未找到运行中的进程"
    exit 0
fi

PID=$(cat "${PID_FILE}")
if ! kill -0 "${PID}" 2>/dev/null; then
    warn "进程 ${PID} 已不存在，清理 PID 文件"
    rm -f "${PID_FILE}"
    exit 0
fi

info "正在停止进程 (PID: ${PID})..."

# 先发 SIGTERM 优雅退出
kill -TERM "${PID}" 2>/dev/null

# 等待最多 10 秒
for i in $(seq 1 10); do
    if ! kill -0 "${PID}" 2>/dev/null; then
        info "进程已优雅退出"
        rm -f "${PID_FILE}"
        exit 0
    fi
    sleep 1
done

# 超时未退出则强制 SIGKILL
warn "进程未响应 SIGTERM，发送 SIGKILL..."
kill -KILL "${PID}" 2>/dev/null && info "已强制停止" || error "停止失败"
rm -f "${PID_FILE}"
