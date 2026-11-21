#!/bin/bash
#
# bin/restart.sh — 重启 OKX 量化交易系统
#
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
echo "[INFO] 正在重启 OKX 量化交易系统..."
"${SCRIPT_DIR}/stop.sh"
sleep 2
"${SCRIPT_DIR}/start.sh" "$@"
