# OKX 合约对冲网格交易框架

> **A production-grade engineering framework for delta-neutral grid trading on OKX perpetual swaps.**
> Focus: order-state correctness, risk gating, and self-healing under real exchange conditions — not on promising returns.

一套面向 OKX 永续合约的**双边对冲网格**交易系统。它不是一个"稳赚策略"，而是一个**把实盘工程问题真正解决过的交易框架**：止盈覆盖丢失后能自愈、账户贴线时知道该放哪个方向、单边趋势里能拦住自我拆解。

---

## ⚠️ 先读这段

- **本项目不构成投资建议，不承诺任何收益。** 网格类策略在震荡市赚小钱、在单边市扛大亏，这是数学结构决定的，不是 bug。
- **作者已用它跑过实盘，并在 2026-11 的极端行情中主动停机。** 仓库里的实测数据是一段有限样本的真实记录，用于展示工程能力与失效边界，**不是业绩展示**。
- 默认配置为**模拟盘**（`flag: '1'`），`ai_trader` 默认关闭。**切换到实盘前，必须按你自己的资金规模重算全部风控参数**（见「参数自洽」一节）。
- 杠杆交易可能导致本金全部损失。请先用模拟盘跑满一个完整行情周期。

---

## 这是什么 / 不是什么

**是：**
- 一个可运行的 OKX V5 永续合约交易系统（多品种 profile 并行、单进程多策略）
- 一套完整的**订单状态一致性**方案：止盈覆盖对账、幽灵层检测、运行时自愈
- 一套**账户级风险闸门**：保证金占用硬/软线、净敞口上限、主导边预算、单边化治理
- 一个能自我验证的**回测框架**（同一份策略逻辑跑历史 1h K 线，含 ADX 趋势命中率统计）
- 一套可运维的工程配套：CommandBus 控制通道、风险哨兵、每日/每周报表、systemd 单元

**不是：**
- 不是高频/低延迟系统（轮询式，秒级）
- 不是多用户 SaaS（有多用户表结构骨架，但未商用、无租户隔离）
- 不是"开箱赚钱"的策略（默认参数只适用于特定资金量与品种）

---

## 核心设计

### 对冲网格：多空双腿各自层叠

每个品种同时维护多空两条腿，各自按固定步长分层加仓，每层独立止盈：

```
        价格
         ↑
   sell ─┤ ← 空腿每层止盈（跌了赚）
         │
  ───────┼─────── 现价
         │
   buy ──┤ ← 多腿每层止盈（涨了赚）
         ↓
```

- 每层名义 = `base_position_usdt × leverage`，层间按 `add_step` 与 `add_multiplier` 递增
- 每层挂独立 `reduce_only` 止盈单，止盈价按层计算（首层 `first_tp_ratio` → 末层 `final_tp_ratio`）
- 价格向某方向走，对应腿持续加仓、另一腿持续止盈 —— 理论上净敞口收敛
- **实际会失效**：单边趋势里逆势腿被止盈平光后不再补仓，系统逐步单边化（见「失效边界」）

### 止盈覆盖与幽灵层自愈（本项目最有价值的部分之一）

实盘最容易致命的不是策略逻辑，而是**本地状态与交易所状态不一致**。典型故障链：

```
本地记录某层止盈单 → 交易所实际已无此单（成交/被撤/重启丢失）
        ↓
覆盖量被虚报为「已覆盖」→ available_qty 恒为 0
        ↓
补挂必失败（qty_below_min）→ 该边止盈永远补不回来
        ↓
该边裸奔，风险敞口无声扩大
```

处理方式：
- `_tp_covered_other_qty()` **只认带订单号的层**，不信任本地数量记录；并以交易所视图兜下限，视图不可用时 fail-closed
- `_heal_missing_tp_coverage()` 独立 120s 定时器：拉交易所活单破除 stale 缓存，对「有持仓但活单止盈≈0」的边重建覆盖
- 再挂（rehang）路径带 `exclude_qty`，把「正在替换的层」排除在已覆盖量之外，避免自己把自己算成已覆盖

> 实测：手动撤掉某边全部止盈单后，120 秒内自动恢复全覆盖，无需重启。

### 账户级风险闸门

| 机制 | 作用 |
|---|---|
| `account_margin_hard_limit` | 冻结率硬线，超过即拒绝一切加仓 |
| `account_margin_soft_limit` | 软线，超线进入降级模式（只减不加） |
| `net_reducing_hard_margin_exempt` | **向零收敛的订单豁免硬线**（见下） |
| `max_net_notional_usdt` | 净敞口上限（真正的风险度量） |
| `throttle_dominant_*` | 主导边加仓封锁 + 净敞口预算闸门 |
| `total_loss_guard` / `black_swan` | 总亏损与极端行情熔断（默认关闭，需自行标定） |
| `liquidation_guard` | 梯度强平距离监控 |

### 单边化死锁的治本：net_reducing 豁免

这是本框架里最"反直觉"的一个设计：

**死锁场景** —— 空单止盈平仓后想补空，但账户冻结率已贴硬线，补空被无差别拦截 → 空边只出不进 → 纯多单边化 → 多头占满保证金 → 冻结率更贴线 → 补空彻底锁死（鸡生蛋）。

**解法** —— 下单前预先计算 `net_before` / `net_after`，若 `|net_after| < |net_before|`（该单让净敞口向零收敛，即"对冲腿方向"），则豁免硬线：

```python
net_reducing = abs(net_after) < abs(net_before)
if net_reducing and self._net_reducing_exempt_enabled:
    # 放行：这个方向在降低风险，不是在加风险
```

**防过度配平**：豁免只在"向零收敛"区间成立。一旦加空越过完全对冲点（净敞口转负），`net_reducing` 自动失效，加空最多摆到净敞口≈0 就停 —— 不会把"治死锁"变成"制造反向裸空"。

该项是 **opt-in**（默认 `false`，fail-closed 保持原行为）。

### MOCA 市场状态识别（默认观察态）

`core/moca_engine.py` 是一个轻量四态分类器：`calm` / `trending_up` / `trending_down` / `spike`。

- 引擎每 30s 调用一次，**默认 `apply_live: false`（只观察、不下单）**
- 观察态会记录状态切换事件，用于后续评估"若上线会触发多少次"
- 真要它生效（自动改杠杆/停逆势腿）属于策略行为重大变更，需单独标定

### CommandBus：不重启改运行时

`control/command_bus.py` 用 SQLite（WAL）做命令队列：INSERT 一行 `status='PENDING'` 即投递，引擎轮询消费。

| 命令 | 效果 |
|---|---|
| `strategy_start` / `strategy_stop` | 启停单个策略（注意：stop 不平仓、不撤单） |
| `risk_resume` / `risk_state_set` | 恢复/强制设定风控状态 |
| `engine_pause` / `engine_resume` | 暂停整个引擎循环 |

---

## 架构

```
main.py                      # 多 profile 入口（glob config/config_*.yaml）
└── core/engine.py           # 主循环：行情 → 策略 → 风控 → 订单
    ├── core/moca_engine.py  # 市场状态分类（观察态）
    ├── strategies/
    │   ├── futures_hedge.py # 策略主体（编排）
    │   └── hedge_kit/       # 按职责拆分的 mixin
    │       ├── tp.py        # 止盈覆盖、再挂、幽灵层自愈（1720 行，最重）
    │       ├── grid.py      # 分层加仓与网格区间
    │       ├── inventory.py # 库存修复、超额回撤
    │       ├── order.py     # 下单/撤单封装与重试
    │       ├── account.py   # 账户快照、保证金口径
    │       ├── risk.py      # 策略侧风险上下文
    │       └── state.py     # 运行时状态持久化
    ├── risk/                # 账户级风控（12 个模块）
    │   ├── policy.py        # 核心策略：硬/软线、shadow_order 预演
    │   ├── position_limits.py / portfolio_budget.py / account_budget.py
    │   ├── black_swan.py / liquidation_guard.py / total_loss_guard.py
    │   └── risk_gate.py / risk_manager.py
    ├── control/command_bus.py   # SQLite 命令通道
    ├── api/okx_client.py        # OKX V5 REST 封装
    ├── backtest/hedge_grid.py   # 回测框架（同源逻辑 + ADX 命中率）
    ├── scripts/                 # 运维/研究/报表脚本
    └── web/                     # FastAPI 面板（可选）
```

代码规模：**约 34,000 行 Python**（不含 50 个测试文件 / 329 个用例）。

---

## 快速开始（模拟盘）

```bash
# 1. 依赖
pip install -r requirements.txt

# 2. 配置 API（首次运行交互输入，加密存 data/keys.enc）
cp config/ai_trader.json.example config/ai_trader.json   # 可选，默认关闭

# 3. 确认 config/config.yaml 中 exchange.okx.flag = '1'（模拟盘）

# 4. 启动
./start.sh
# 或
python main.py
```

**务必先跑模拟盘**，观察：止盈是否全覆盖、加仓是否被合理拦截、净敞口是否收敛。

---

## 参数自洽（最容易踩的坑）

风控参数不是随便填的，它们之间有硬约束：

```
account_margin_hard_limit ≥ max_net_notional_usdt ÷ leverage ÷ equity
```

**不满足这条，净敞口上限形同虚设** —— 因为加仓会先被保证金硬线拦死，永远到不了净敞口上限。

另外注意两个语义陷阱：
- `throttle_dominant_gap_ratio` 默认 `0` 意味着"几乎总判主导边、永远封一边"；配成 `1.0` 等于关闭封锁。**两个极端值含义完全相反。**
- **冻结率（占用率）不是风险度量。** 纯对冲结构冻结率 70–80% 仍安全；单边结构 40% 就可能很危险。真正的风险度量是 **有效杠杆 = 净敞口 ÷ 权益**。

示例配置以约 1k USDT 量级的账户为基准，换资金量请整体重算。

---

## 实测记录（有限样本，非业绩承诺）

单账户、约 1 个月、三品种（CRV / FARTCOIN / RAM）：

| 品种 | 已实现净盈亏 | 备注 |
|---|---|---|
| RAM | +477 U | 主力盈利 |
| CRV | +81 U | 2691 笔（大量零散减仓） |
| FARTCOIN | −196 U | 单边下跌中空腿被止盈平光，转裸多 |
| 三品种合计 | **+363 U** | 手续费是主要成本项 |

回测（1h × 90 天）里最有价值的一条结论：**"趋势中停掉逆势腿加仓"是单一最有效的参数杠杆** —— RAM 从 +44 U 改善到 +98 U（回撤 2.2% → 1.6%），CRV 从 −55 U 改善到 −28 U。

ADX 趋势判断命中率（前向验证，`TREND_ADX=25`）：

| 品种 | 对称命中率 | 逆势腿犯错率 |
|---|---|---|
| CRV | 61.0% | 30.2% |
| FARTCOIN | 65.8% | 34.4% |
| RAM | 66.0% | 32.2% |

**这些数字的正确读法**：ADX 只有六成准，但"停逆势加仓"有七成是对的，而它挡住的是最致命的单边化亏损 —— 它是安全带，不是油门。

---

## 失效边界（诚实记录）

这套系统在 2026-11 的行情里失效并停机，原因值得写下来：

1. **单边趋势会拆解对称网格。** 上涨把多腿止盈平光 → 裸空；下跌把空腿止盈平光 → 裸多。两种方向都实测发生过。net_reducing 豁免能治"补不进去"，治不了"价格不回来"。
2. **浮亏会超过权益。** 靠本金缓冲扛杠杆头寸时，一旦趋势持续，回撤会击穿心理与资金双重底线。
3. **样本不足。** 一个月、一个账户、一种行情，不足以证明任何长期稳定性。
4. **手续费是隐性杀手。** 双边对冲 + 高频分层，手续费可能吃掉大部分毛利。

**结论**：均值回归网格不是"能不能写对"的问题，是"行情配合不配合"的问题。这套代码解决的是前者，无法解决后者。

---

## 扩展品种

```bash
cp config/config_fartcoin.yaml config/config_sol.yaml
# 修改 symbol / ctVal 相关参数 / 风控阈值
# 重启服务即可（main.py 用 glob 加载 config/config_*.yaml）
```

注意不同品种的 `ctVal`（合约面值）不同，**名义价值一律取交易所返回的 `notionalUsd`，不要用 `qty × markPx` 自算**。

---

## 测试

```bash
pytest -q          # 50 个测试文件 / 329 个用例，覆盖止盈覆盖、单边化豁免、命令总线、风控放行路径
```

> 实测：`325 passed, 4 skipped`（4 个 skip 为已退役品种配置的校验，可安全忽略）。

---

## 已知限制

- 轮询式（秒级），不适合高频
- 单进程多策略，共享一个账户保证金，品种间存在隐性耦合
- 多用户骨架未做租户隔离与密钥强加密（当前为 XOR/基础加密，**不可商用**）
- Web 面板无 HTTPS、URL query 传 token，仅限内网/隧道使用
- 无 WebSocket 行情（`api/ws_client.py` 为预留）

---

## License

MIT —— 见 [LICENSE](LICENSE)。

## 免责声明

本项目仅供学习与研究。加密货币合约交易具有极高风险，可能导致本金全部损失。作者不对任何使用本项目产生的亏损承担责任。请自行评估风险、遵守所在司法辖区的法律法规。**不要用你无法承受损失的资金运行任何交易系统。**
