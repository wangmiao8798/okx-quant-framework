#!/bin/bash
#
# bin/setup.sh — Ubuntu 环境一键部署脚本
#
# 用法:
#   sudo ./bin/setup.sh              # 完整部署至 /opt/okx_quant
#   sudo ./bin/setup.sh --dry-run    # 仅检查环境，不实际安装
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

DRY_RUN=false
[[ "$1" == "--dry-run" ]] && DRY_RUN=true

if $DRY_RUN; then
    info "===== 运行环境预检 ====="
else
    info "===== Ubuntu 环境部署 ====="
    info "目标目录: /opt/okx_quant"
fi

# ---- 1. 系统包检查 ----
install_system_packages() {
    info "[1/5] 检查系统依赖..."
    for cmd in python3 git curl; do
        if command -v "${cmd}" &>/dev/null; then
            info "  ${cmd}: OK ($(command -v ${cmd}))"
        else
            if $DRY_RUN; then
                warn "  ${cmd}: 缺失 (需要 apt install)"
            else
                info "  正在安装 ${cmd}..."
                sudo apt update -qq
                sudo apt install -y python3 python3-pip git curl
                break
            fi
        fi
    done
}

# ---- 2. Python 依赖 ----
install_python_deps() {
    info "[2/5] 安装 Python 依赖..."
    if command -v python3 &>/dev/null; then
        ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        info "  Python 版本: ${ver}"
        if $DRY_RUN; then
            info "  依赖列表:"
            cat "${PROJECT_DIR}/requirements.txt" | while read -r line; do echo "    ${line}"; done
        else
            python3 -m pip install --upgrade pip --quiet
            python3 -m pip install -r "${PROJECT_DIR}/requirements.txt" --quiet
            info "  依赖安装完成"
        fi
    else
        error "  python3 未安装，请执行: sudo apt install -y python3 python3-pip"
        exit 1
    fi
}

# ---- 3. 目录部署 ----
deploy_dirs() {
    info "[3/5] 创建部署目录..."
    if $DRY_RUN; then
        info "  源目录: ${PROJECT_DIR}"
        info "  目标目录: /opt/okx_quant"
        info "  数据目录: /opt/okx_quant/data"
        info "  日志目录: /opt/okx_quant/logs"
    else
        if [ "${PROJECT_DIR}" != "/opt/okx_quant" ]; then
            sudo cp -r "${PROJECT_DIR}" /opt/okx_quant
            info "  项目已复制到 /opt/okx_quant"
            PROJECT_DIR="/opt/okx_quant"
        fi
        sudo mkdir -p /opt/okx_quant/data /opt/okx_quant/logs
        sudo chmod 755 /opt/okx_quant/bin/*.sh
    fi
}

# ---- 4. 创建系统服务 ----
install_systemd() {
    info "[4/5] 注册 systemd 服务..."
    if $DRY_RUN; then
        info "  服务文件: systemd/okx-quant.service -> /etc/systemd/system/"
        info "  启动命令: systemctl enable --now okx-quant"
    else
        sudo cp "${PROJECT_DIR}/systemd/okx-quant.service" /etc/systemd/system/
        sudo systemctl daemon-reload
        info "  服务文件已安装 (okx-quant.service)"
        info "  启用: sudo systemctl enable okx-quant"
        info "  启动: sudo systemctl start okx-quant"
    fi
}

# ---- 5. 配置 logrotate ----
install_logrotate() {
    info "[5/5] 配置日志轮转..."
    if $DRY_RUN; then
        info "  配置文件: logrotate/okx-quant -> /etc/logrotate.d/"
    else
        sudo cp "${PROJECT_DIR}/logrotate/okx-quant" /etc/logrotate.d/
        info "  日志轮转已配置"
    fi
}

firewall_tips() {
    echo ""
    info "===== 部署完成 ====="
    echo ""
    echo "  启动方式一（systemd 推荐）:"
    echo "    sudo systemctl daemon-reload"
    echo "    sudo systemctl enable okx-quant"
    echo "    sudo systemctl start okx-quant"
    echo ""
    echo "  启动方式二（手动）:"
    echo "    cd /opt/okx_quant"
    echo "    ./bin/start.sh"
    echo ""
    echo "  常用命令:"
    echo "    systemctl status okx-quant      # 查看服务状态"
    echo "    journalctl -u okx-quant -f      # 查看实时日志"
    echo "    tail -f /opt/okx_quant/logs/trading.log"
    echo "    /opt/okx_quant/bin/status.sh    # 查看状态（手动模式）"
    echo ""
    info "提示: 如果使用 systemd 启动，PID 文件不在 data/ 下，用 journalctl 查看日志"
}

install_system_packages
install_python_deps
deploy_dirs
install_systemd
install_logrotate
firewall_tips
