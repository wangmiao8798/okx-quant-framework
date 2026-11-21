# 单方向趋势系统架构契约

状态：设计冻结前审查稿
日期：2026-08-24
负责人：Hermes（架构、风险决策、服务器验收）
实现协作：Codex 仅执行明确拆分的机械编码

## 1. 目标

建立一个面向全市场 USDT 永续合约的单方向趋势策略系统。它根据独立采集的行情、流动性和波动数据选择交易对与方向，单笔开仓，禁止马丁/摊平/双向库存修复，并在开仓保护确认后才进入受保护持仓状态。

本系统不是现有双向网格的一个 profile，也不是 `FuturesHedgeStrategy` 的配置分支。

## 2. 与双向系统的硬隔离

单方向系统不得复用或读写以下双向策略内部资源：

- 双向网格配置：`config/config.yaml`、`config/config_dram.yaml`、`config/config_fartcoin.yaml`；
- 双向策略状态、网格层、库存、均价、弱侧修复状态；
- 双向 TP 状态和网格订单恢复状态；
- 双向策略的信号判断、趋势偏置、配对开仓、inventory repair；
- 双向策略的策略级 PnL 和报告；
- 双向主状态文件和双向策略数据库表。

单方向系统必须拥有独立的：

- 配置文件：`config/one_way.yaml`；
- 运行目录：`one_way/`；
- 影子/状态数据库：`data/one_way/one_way_shadow.db`（或独立目录下的等价数据库）；
- 订单意图命名空间：`one_way_*`；
- 持仓标识：独立 `strategy_id`、`trade_id`、`clOrdId` 前缀；
- PnL、成交、风险和报告记录。

第一阶段不得让单方向模块写入现有 `data/trading.db`，避免污染和 SQLite 写竞争。

## 3. 模块边界

```text
one_way/
  market/       全市场枚举、行情采集、标准化、数据新鲜度
  signal/       多空独立评分、候选排名、信号生命周期
  risk/         单笔风险、仓位、ATR 止损/止盈、冷却、策略槽位
  portfolio/    只读账户硬风险适配器；不做市场判断
  execution/    单方向订单提交、成交确认、幂等和保护单协调
  protection/   交易所 TP/SL 预设、覆盖检查、裸仓 fail-closed
  state/        独立状态模型和恢复
  shadow/       影子信号/理论成交/回放
  reports/      单方向独立报告
```

## 4. 允许共享的唯一边界

只能共享账户级最终硬风险数据/接口，不共享策略判断：

```text
one_way signal + one_way risk
  -> portfolio hard-risk adapter
  -> allow / reject
  -> one_way execution
```

适配器可读取：

- 账户权益、可用保证金、IMR/MMR、mgnRatio；
- 账户总保证金和总名义预算；
- 全账户硬停止状态；
- 数据新鲜度和交易所可用性；
- 已有组合相关性/风险占用。

适配器不得输出或决定：

- 做多还是做空；
- 交易哪个币；
- 入场信号；
- 趋势是否成立；
- 止盈策略的方向判断；
- 双向网格均价或库存修复。

## 5. 订单意图命名空间

单方向不得使用 `normal_grid`、`inventory_repair` 或 `paired_net_neutral`。

预留意图：

- `one_way_entry_long`
- `one_way_entry_short`
- `one_way_take_profit`
- `one_way_stop_loss`
- `one_way_time_exit`
- `one_way_trend_exit`
- `one_way_emergency_reduce`

所有意图必须在单方向模块、最终风险适配器、执行记录和报告中保持一致；未知或冲突意图 fail-closed。

## 6. 单方向持仓不变量

每一笔单方向交易必须包含：

- 唯一 `trade_id`；
- symbol、direction、entry plan；
- 数量、合约价值、杠杆、保证金模式；
- 入场价、硬止损价、止盈价；
- 最大亏损金额和最大名义；
- TP/SL 的客户端和交易所订单标识；
- 当前保护状态。

同一交易对同一时间最多一个单方向趋势仓；首版禁止补仓、马丁、摊平和直接反手。

## 7. 开仓即保护

优先使用交易所支持的原子附带 TP/SL。若接口不支持：

```text
生成 entry + TP + SL 计划
-> 账户硬风险检查
-> entry 成交确认
-> 读取真实成交数量/均价
-> 立即提交 reduce-only TP 和交易所保护 SL
-> 查询并确认两者 live、数量覆盖完整
-> 才标记 POSITION_PROTECTED
```

任何保护单失败、数量不足、价格方向错误、数据过期或交易所返回不确定：

- 禁止下一笔新开仓；
- 不得把失败保护转化为普通订单；
- 进入独立裸仓应急处理；
- 直到保护覆盖恢复，才允许恢复策略。

## 8. 状态机

```text
DISABLED
  -> OBSERVING
  -> SHADOW
  -> CANDIDATE
  -> ENTRY_APPROVED
  -> ENTRY_SUBMITTED
  -> ENTRY_FILLED_UNPROTECTED
  -> POSITION_PROTECTED
  -> EXIT_SUBMITTED
  -> CLOSED
  -> COOLDOWN
```

任何状态异常、重启后状态不一致、交易所订单不可确认，都进入 `FAIL_CLOSED`，不得继续开仓。

## 9. 配置隔离要求

`one_way.yaml` 只允许单方向字段，例如：

- universe/filter 参数；
- 多周期信号参数；
- 波动率和流动性门槛；
- 单笔风险预算；
- 最大名义、最大槽位；
- ATR 止损/止盈和时间止损；
- shadow/live feature flag；
- 独立状态目录和报告目录。

不得复制双向字段：

- `max_add_layers`；
- `add_multiplier`；
- `inventory_repair_*`；
- `paired_add_*`；
- `grid_*`；
- `long_grid` / `short_grid`；
- 双向库存和均价字段。

## 10. 部署与启动隔离

单方向系统不得由当前 `core/engine.py` 的额外双向配置加载路径启动。设计目标是独立入口和独立 systemd 服务，例如：

```text
one-way-shadow.service
```

首阶段只运行 shadow/observer；不接入 `okx-quant.service`，不修改其启动参数，不共享其生命周期。

单方向服务必须有独立日志、PID、状态目录和数据库连接。任何一个服务停止、重启或锁库，都不能停止或改变另一个服务的策略状态。

## 11. 验收门槛

在任何实盘接入前必须证明：

1. 单方向代码不会导入双向策略或双向配置；
2. 双向系统启动时不会发现或加载 `one_way.yaml`；
3. 单方向 shadow 不写 `data/trading.db`；
4. 两套系统的订单意图、clOrdId、状态目录和报告可区分；
5. 单方向保护单数量覆盖真实仓位；
6. 单方向服务重启不会触发双向服务重启；
7. 双向服务重启不会丢失单方向 shadow 状态；
8. 账户硬风险拒绝能阻断开仓，但不会改变单方向信号判断；
9. 单方向的历史回放、服务器影子运行和压力测试通过；
10. 任何未验证项均标记为未完成，不得以本地测试替代服务器证据。

## 12. 当前决策

当前只保留独立纯评分器作为候选算法组件；不接入现有引擎、不下单、不写主数据库。下一步先实现独立模块契约测试和服务器 shadow 入口，完成隔离验收后再研究执行路径。
