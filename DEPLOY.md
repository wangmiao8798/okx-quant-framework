# OKX 量化交易系统 — Ubuntu 部署指南

## 目录

1. [系统要求](#1-系统要求)
2. [快速部署](#2-快速部署)
3. [手动安装](#3-手动安装)
4. [配置指南](#4-配置指南)
5. [运行管理](#5-运行管理)
6. [日志与监控](#6-日志与监控)
7. [安全加固](#7-安全加固)
8. [故障排除](#8-故障排除)
9. [架构说明](#9-架构说明)
10. [发布基线与回滚](#10-发布基线与回滚)

---

## 1. 系统要求

### 硬件
- CPU: 1 核及以上（推荐 2 核）
- 内存: 512 MB 及以上（推荐 1 GB）
- 磁盘: 1 GB 可用空间
- 网络: 稳定互联网连接（建议低延迟）

### 软件
- 操作系统: **Ubuntu 22.04 / 24.04 LTS**（系统预装 Python 3.10+）
- Python: **3.8+**（Ubuntu 22.04 预装 Python 3.10）
- 依赖: git, curl

### 端口
- 本系统为纯客户端运行，不监听任何端口
- 仅需出站网络连接至 OKX 交易所 API

---

## 2. 快速部署

### 一键安装（推荐）

```bash
# 1. 获取代码
git clone <your-repo-url> /opt/okx_quant
cd /opt/okx_quant

# 2. 运行 Ubuntu 部署脚本
sudo ./bin/setup.sh

# 3. 编辑配置文件
vim config/config.yaml

# 4. 启动系统
systemctl enable okx-quant
systemctl start okx-quant
```

setup.sh 会自动完成：系统依赖检查、Python 依赖安装、systemd 服务注册、logrotate 配置。

### 从副本部署

若已在本机准备好项目文件夹：

```bash
# 复制到 /opt
sudo cp -r <source_path>/okx_quant /opt/

# 运行部署脚本
sudo /opt/okx_quant/bin/setup.sh

# 启动
systemctl start okx-quant
```

---

## 3. 手动安装

### 3.1 系统依赖

```bash
# Ubuntu 22.04 — 安装 Python 3.8+
sudo apt install -y Ubuntu-release-scl
sudo apt install -y python3-pip
scl enable python3 bash

# Ubuntu 24.04 — Python 3 通常已预装
sudo apt install -y python3 python3-pip git

# 验证版本
python3 --version  # 需为 3.8+

# 安装开发工具（编译依赖用）
sudo apt install -y "build-essential python3-dev"
```

### 3.2 Python 依赖

```bash
cd /opt/okx_quant

# 升级 pip
python3 -m pip install --upgrade pip

# 安装项目依赖
python3 -m pip install -r requirements.txt

# 验证
python3 -c "import yaml, cryptography, okx; print('OK')"
```

### 3.3 创建目录结构

```bash
mkdir -p /opt/okx_quant/{data,logs}
chmod 755 /opt/okx_quant/bin/*.sh
```

### 3.4 注册 systemd 服务

```bash
# 复制服务文件
sudo cp /opt/okx_quant/systemd/okx-quant.service /etc/systemd/system/

# 重新加载服务
sudo systemctl daemon-reload

# 启用开机自启
sudo systemctl enable okx-quant

# 启动服务
sudo systemctl start okx-quant

# 查看状态
sudo systemctl status okx-quant
```

### 3.5 配置日志轮转

```bash
sudo cp /opt/okx_quant/logrotate/okx-quant /etc/logrotate.d/
sudo logrotate -f /etc/logrotate.d/okx-quant  # 测试
```

---

## 4. 配置指南

### 4.1 基本配置

编辑 `/opt/okx_quant/config/config.yaml`：

```yaml
exchange:
  okx:
    key_file: "data/keys.enc"    # 加密密钥存储路径
    flag: "1"                    # "1"=实盘, "0"=测试网
    proxy: ""                    # HTTP 代理（如需）
```

建议参数调整：

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| `flag` | `"0"` | 首次运行先用测试网验证 |
| `tick_interval` | `3` | 轮询间隔（秒），Ubuntu 低配可设为 5 |
| `invest_usdt` | `50` | 起始投入 USDT，先小量测试 |
| `leverage` | `2` | 合约杠杆，建议从低倍开始 |

### 4.2 环境变量（可选）

系统会从加密文件读取 API 密钥，无需环境变量。首次运行需交互输入。

---

## 5. 运行管理

### 5.1 方式一：systemd（推荐，生产环境）

```bash
# 启动
sudo systemctl start okx-quant

# 停止
sudo systemctl stop okx-quant

# 重启
sudo systemctl restart okx-quant

# 查看状态
sudo systemctl status okx-quant

# 开机自启
sudo systemctl enable okx-quant

# 禁用自启
sudo systemctl disable okx-quant
```

### 5.2 方式二：脚本管理（开发/调试）

```bash
cd /opt/okx_quant

# 启动（交互式输入密钥）
./bin/start.sh

# 演示模式
./bin/start.sh --demo

# 启动（使用已保存的加密密钥）
./bin/start.sh --password "your_password"

# 停止
./bin/stop.sh

# 重启
./bin/restart.sh

# 查看状态
./bin/status.sh
```

### 5.3 首次运行流程

```bash
# 1. 进入目录
cd /opt/okx_quant

# 2. 编辑配置（建议先用测试网）
vim config/config.yaml
# 修改 flag: "0"

# 3. 启动（交互输入 API 密钥）
./bin/start.sh

# 4. 按提示输入 OKX API Key / Secret / Passphrase
# 5. 选择是否加密保存密钥

# 6. 观察日志
tail -f logs/trading.log

# 7. 确认正常运行后，改用 systemd 生产部署
```

> **注意**: 首次运行 `start.sh` 会在 `data/` 下创建加密密钥文件 `keys.enc`。
> 加密口令请妥善保管，丢失无法恢复！

---

## 6. 日志与监控

### 6.1 日志位置

| 内容 | 路径 |
|------|------|
| 交易日志 | `/opt/okx_quant/logs/trading.log` |
| 启动日志 | `/opt/okx_quant/logs/startup.log` |
| 错误日志（systemd） | `journalctl -u okx-quant -e` |
| 盈亏记录 | `/opt/okx_quant/data/pnl_records.csv` |

### 6.2 查看日志

```bash
# 实时交易日志
tail -f /opt/okx_quant/logs/trading.log

# 最近 100 行
tail -100 /opt/okx_quant/logs/trading.log

# systemd 日志（仅 systemd 启动时）
journalctl -u okx-quant -f

# 搜索关键字
grep "ERROR\|FATAL\|异常" /opt/okx_quant/logs/trading.log
```

### 6.3 日志轮转

通过 logrotate 自动轮转，每天切分，保留 30 天，单文件最大 50MB。

```bash
# 手动触发轮转
sudo logrotate -f /etc/logrotate.d/okx-quant
```

---

## 7. 安全加固

### 7.1 API 密钥安全

- 仅授予 **交易权限**，禁止开通提币权限
- 密钥使用 PBKDF2 + Fernet 加密存储在本地 `data/keys.enc`
- 加密口令建议使用 16 位以上随机字符串
- 不要将密钥文件提交到 Git

### 7.2 操作系统安全

```bash
# 创建专用用户
sudo useradd -r -s /sbin/nologin okxquant
sudo chown -R okxquant:okxquant /opt/okx_quant
# 注意：如果使用 systemd，需修改 service 文件中的 User

# 防火墙（本系统不监听端口，仅出站）
sudo ufw --permanent --add-rich-rule='rule family="ipv4" source address="0.0.0.0/0" port port="443" protocol="tcp" accept'
sudo ufw --reload



# 或直接设为宽松模式（不推荐生产）

```

### 7.3 文件权限

```bash
# 密钥文件仅所有者可读写
chmod 600 /opt/okx_quant/data/keys.enc

# 日志和配置文件
chmod 640 /opt/okx_quant/config/config.yaml
chmod 640 /opt/okx_quant/logs/*.log

# 脚本为可执行
chmod 755 /opt/okx_quant/bin/*.sh
```

### 7.4 系统资源限制

```bash
# /etc/security/limits.conf 或 /etc/systemd/system.conf
# 推荐限制：
# DefaultLimitNOFILE=65536
# DefaultLimitNPROC=1024
```

---

## 8. 故障排除

### 8.1 Python 版本问题

**症状**: `python3` 未找到或版本 < 3.8

**解决**:
```bash
# Ubuntu 22.04
sudo apt install -y Ubuntu-release-scl
sudo apt install -y python3
scl enable python3 bash

# Ubuntu 8/Stream
sudo apt install -y python3
```

### 8.2 依赖安装失败

**症状**: `pip install` 报错

**解决**:
```bash
# 先安装系统编译工具
sudo apt install -y "build-essential python3-dev"

# 再重试
python3 -m pip install --upgrade pip setuptools wheel
python3 -m pip install -r requirements.txt
```

### 8.3 API 连接失败

**症状**: 日志提示 "API连接失败" 或网络超时

**排查**:
```bash
# 1. 检查网络
curl -s https://www.okx.com/api/v5/public/time

# 2. 检查配置
grep "flag" /opt/okx_quant/config/config.yaml
# flag "0" = 测试网, flag "1" = 实盘

# 3. 测试网地址为 https://www.okx.com 不变
#    实盘也为相同地址，仅 flag 参数区分
```

### 8.4 系统无响应

**症状**: 系统卡死或内存溢出

**解决**:
```bash
# 1. 强制停止
sudo systemctl stop okx-quant  # 或
kill -9 $(cat /opt/okx_quant/data/run.pid)

# 2. 检查系统资源
top -b -n 1 | head -20
free -h

# 3. 调整 tick_interval 降低轮询频率
```

---

## 9. 架构说明

```
/opt/okx_quant/
├── bin/                       # 管理脚本
│   ├── start.sh              #   一键启动
│   ├── stop.sh               #   优雅停止
│   ├── restart.sh            #   重启
│   ├── status.sh             #   状态查询
│   └── setup.sh              #   环境部署
├── config/
│   └── config.yaml           #   全局配置文件
├── core/
│   ├── engine.py             #   主引擎（流程编排）
│   └── moca_engine.py        #   MOCA 自适应引擎
├── strategies/
│   ├── base.py               #   策略基类
│   ├── spot_grid.py          #   现货币币策略
│   └── futures_hedge.py      #   合约对冲策略
├── api/
│   ├── okx_client.py         #   OKX V5 REST API
│   ├── ws_client.py          #   WebSocket（预留）
│   └── crypto_utils.py       #   加密存储
├── risk/
│   ├── black_swan.py         #   黑天鹅熔断
│   ├── liquidation_guard.py  #   强平风控
│   └── risk_manager.py       #   风控总控
├── utils/
│   ├── helpers.py            #   技术指标 + 工具函数
│   ├── logger.py             #   日志系统
│   └── recorder.py           #   盈亏记录
├── data/                     #   运行时数据（密钥、PID、快照）
├── logs/                     #   运行日志
├── systemd/
│   └── okx-quant.service     #   systemd 服务单元
├── logrotate/
│   └── okx-quant             #   日志轮转配置
├── main.py                   #   程序入口
├── requirements.txt
├── .gitignore
└── DEPLOY.md                 #   本文件
```

### 9.1 运行时分层

生产订单必须经过同一条执行边界：

```text
策略层（CRV / DRAM / FARTCOIN，多空独立参数）
  -> ExecutionGateway
  -> RiskGate / RiskPolicy + AccountBudgetAllocator
  -> EventLedger + ExchangeReconciler
  -> OKX V5
```

`inventory/` 只负责双向仓位和弱侧修复计算；`risk/` 负责账户快照、保证金预算、净敞口和硬线；`control/` 负责人工恢复与状态命令。策略可以跳过候选订单，但不能绕过 `ExecutionGateway` 直接写交易所。实盘期货启动时，账本、首轮对账和 `RiskGate` 任一不可用都会 fail-closed。

账户级风险数据由引擎统一刷新并供三币策略共享，避免不同策略使用不同时间点的权益或保证金快照。普通网格、库存修复和配对加仓是不同的订单意图，分别接受相应的额度和净敞口检查。

### 9.2 Web 管理面板

`okx-quant-web.service` 运行 `web.main:app`，仅监听 `127.0.0.1:8799`；Nginx 监听公网 `8798` 并反向代理到该内部端口。Web 只通过 API 读取状态、报表和控制命令；交易提交仍由量化服务的执行网关完成。Web 服务使用独立的 systemd 沙箱权限，不读取 `data/keys.enc`。

### 9.3 定时任务

以下单元由 `deploy/systemd/` 提供，安装后应确认 timer 为 `enabled` 且 `active`：

| Timer | 周期 | 作用 |
|---|---:|---|
| `okx-activity-reports.timer` | 15 分钟 | 刷新订单活动、资金效率和 round-trip 报表 |
| `okx-cross-market-opportunity.timer` | 5 分钟 | 只读跨市场机会研究，不下单 |
| `okx-dram-inventory-shadow.timer` | 1 分钟 | 只读 DRAM 库存修复策略影子比较 |

### 9.4 测试与预检

在发布目录运行：

```bash
cd /opt/okx_quant
PYTHONPATH=/opt/okx_quant:/opt/okx_quant/web venv/bin/pytest -q
python3 bin/preflight.py
```

测试必须全绿后才允许重启服务。配置检查应覆盖 YAML 解析、三币账户级字段一致性、通知密码不入库、systemd 文件和实盘 fail-closed 条件。

## 10. 发布基线与回滚

### 10.1 发布前备份

备份必须位于仓库外的管理员目录，例如 `/opt/okx_quant_release_backups/<timestamp>/`，权限 `0700`，至少包括：

- 源码工作树归档、`git status`、HEAD 和补丁；
- SQLite `trading.db` 一致性快照；
- systemd 单元、服务状态和配置校验结果；
- 每个归档的 SHA256。

`rollback/`、`.deploy_staging_*`、`*.orig` 和 `*.bak*` 只允许留在上述外部备份目录，不提交到 Git。

### 10.2 安全部署顺序

```bash
cd /opt/okx_quant
PYTHONPATH=/opt/okx_quant:/opt/okx_quant/web venv/bin/pytest -q
sudo systemctl daemon-reload
sudo systemctl restart okx-quant-web.service
sudo systemctl restart okx-quant.service
systemctl is-active okx-quant.service okx-quant-web.service
```

重启前再次确认备份哈希、工作区干净、通知环境文件 `/etc/okx-quant/notifications.env` 为 `root:root`、`0600`。量化服务重启后检查 RiskGate、ExchangeReconciler、TP 覆盖和账户快照；异常时先停止新增开仓，再按备份回滚。

### 10.3 回滚

```bash
sudo systemctl stop okx-quant.service
sudo systemctl stop okx-quant-web.service
# 从外部发布归档恢复源码和数据库，核对 SHA256
sudo systemctl daemon-reload
sudo systemctl start okx-quant-web.service
sudo systemctl start okx-quant.service
```

回滚只恢复经过校验的源码和数据库快照，不删除当前归档；恢复后必须重新执行全量测试和服务状态检查。

---

> **免责声明**: 加密货币交易具有高风险。本程序仅供学习和研究，不构成投资建议。
> 使用前请充分测试，建议先用测试网模式运行确认逻辑无误。
