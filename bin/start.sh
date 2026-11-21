#!/bin/bash
# bin/start.sh - OKX Quant System startup
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

PID_FILE="${PROJECT_DIR}/data/run.pid"
LOG_DIR="${PROJECT_DIR}/logs"
DATA_DIR="${PROJECT_DIR}/data"
CONFIG_FILE="${PROJECT_DIR}/config/config.yaml"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

detect_python() {
    if [ -f /opt/okx_quant/venv/bin/python ]; then
        PYTHON=/opt/okx_quant/venv/bin/python
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON=python3
    else
        error "Python not found"
        exit 1
    fi
    info "Python: $PYTHON"
}

check_env() {
    mkdir -p "${LOG_DIR}" "${DATA_DIR}"
    if [ ! -f "${CONFIG_FILE}" ]; then
        error "Config not found: ${CONFIG_FILE}"
        exit 1
    fi
    if [ -f "${PID_FILE}" ]; then
        OLD_PID=$(cat "${PID_FILE}")
        if kill -0 "${OLD_PID}" 2>/dev/null; then
            warn "Already running (PID: ${OLD_PID})"
            exit 0
        fi
        rm -f "${PID_FILE}"
    fi
}

start_program() {
    info "Starting OKX Quant System..."
    info "Log: ${LOG_DIR}/trading.log"
    CMD="${PYTHON} main.py --config ${CONFIG_FILE} $*"
    nohup ${CMD} > "${LOG_DIR}/startup.log" 2>&1 &
    PID=$!
    echo "${PID}" > "${PID_FILE}"
    info "Started (PID: ${PID})"
    sleep 2
    if kill -0 "${PID}" 2>/dev/null; then
        info "Running normally"
    else
        warn "May have exited, check: tail -50 ${LOG_DIR}/startup.log"
    fi
}

detect_python
check_env
start_program "$@"
