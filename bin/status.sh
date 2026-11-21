#!/bin/bash
#
# bin/status.sh — 查看 OKX 量化交易系统运行状态
#
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="${PROJECT_DIR}/data/run.pid"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

if [ -f "${PID_FILE}" ]; then
    PID=$(cat "${PID_FILE}")
    if kill -0 "${PID}" 2>/dev/null; then
        echo -e "${GREEN}[运行中]${NC} PID: ${PID}"
        echo "  项目目录: ${PROJECT_DIR}"
        RUNTIME=$(ps -o etime= -p "${PID}" 2>/dev/null | tr -d ' ')
        echo "  运行时长: ${RUNTIME:-N/A}"
        MEM=$(ps -o rss= -p "${PID}" 2>/dev/null | tr -d ' ')
        echo "  内存使用: $((MEM / 1024)) MB"
        echo "  日志文件: ${PROJECT_DIR}/logs/trading.log"
        echo ""
        echo "  查看实时日志: tail -f ${PROJECT_DIR}/logs/trading.log"
    else
        echo -e "${YELLOW}[已停止]${NC} PID 文件存在但进程已退出"
        echo "  残留 PID: ${PID}"
        echo "  清理: rm -f ${PID_FILE}"
    fi
else
    if pgrep -f "main.py" > /dev/null 2>&1; then
        echo -e "${YELLOW}[运行中]${NC} 未找到 PID 文件，但存在 main.py 进程"
        pgrep -af "main.py"
    else
        echo -e "${RED}[已停止]${NC}"
    fi
fi
