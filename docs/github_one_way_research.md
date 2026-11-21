# GitHub 单方向系统参考调研

日期：2026-08-24
用途：为独立 one_way 系统提供架构参考；不代表任何第三方代码已经接入或经过本服务器验证。

## 结论

没有找到一个可以直接安全搬进当前 OKX 双向网格系统的完整项目，同时满足：全市场动态选币、单方向趋势、OKX 永续、开仓保护 TP/SL、独立状态/配置/执行、组合硬风险适配器。

采用“机制借鉴，不整仓迁移”：

1. Hummingbot：首选参考 OKX 永续连接器、方向型 Controller、订单跟踪和多策略隔离思想。
2. Freqtrade：首选参考动态 Pairlist、策略/保护机制、回测/影子验证、exchange stoploss 设计；但 GPL-3.0，不能未经许可证审查复制进现有项目。
3. NautilusTrader：参考事件驱动架构、Strategy/Execution 分离、订单保护和确定性事件模型；体量大，暂不迁移。
4. Jesse：参考单策略生命周期、Position 模型、同时管理 TP/SL 的测试思路；不作为 OKX 执行底座。
5. OKX Agent Trade Kit：只参考 OKX API/账户工具边界；它主要是 OKX 原生 Grid/DCA bot 管理，不是本需求的单方向趋势系统。

## 重要仓库

- Freqtrade（GPL-3.0，约 53k stars）：https://github.com/freqtrade/freqtrade
- Hummingbot（Apache-2.0，约 19k stars）：https://github.com/hummingbot/hummingbot
- NautilusTrader（LGPL-3.0，约 27k stars）：https://github.com/nautechsystems/nautilus_trader
- Jesse（MIT，约 8k stars）：https://github.com/jesse-ai/jesse
- OKX Agent Trade Kit（MIT，约 96 stars）：https://github.com/dex-original/okx-agent-trade-kit

## 可借鉴机制

### Hummingbot

GitHub 路径已确认：

- `hummingbot/connector/derivative/okx_perpetual/`
- `controllers/directional_trading/`
- `hummingbot/connector/client_order_tracker.py`
- `hummingbot/connector/budget_checker.py`

适合借鉴：

- OKX 永续市场数据、认证、用户流和订单适配边界；
- directional controller 与执行连接器分离；
- client order tracker、订单状态机、部分成交处理；
- 每个 controller 独立配置和运行状态。

不直接搬：Hummingbot 面向做市/高频，执行模型和当前趋势策略不完全相同。

### Freqtrade

已确认机制：

- 动态 Pairlist：Volume、PercentChange、CrossMarket、Age、Delist、Spread、Volatility 等过滤器；
- Stoploss on exchange：开仓成交后立即在交易所设置保护单；
- exchange feature matrix：区分交易所/现货/期货/保证金模式支持的 stop 类型；
- Protections：StoplossGuard、MaxDrawdown、Cooldown、LowProfitPairs，并支持 per-pair/per-side 限制；
- 回测与 protection 显式启用。

适合借鉴：

- 全市场 universe 筛选管道；
- 信号、保护、冷却和回测分层；
- 交易所能力先验检查；
- stoploss on exchange 的补挂和失败处理。

必须注意：GPL-3.0，不能直接复制核心代码进入当前项目而不做许可证审查。

### NautilusTrader

已确认机制：

- Rust/Python 事件驱动交易引擎；
- `Strategy` 与执行/适配器分离；
- `crates/execution/src/protection.rs` 的保护价格计算；
- OKX adapter；
- EMA cross、bracket order 等独立示例；
- 强调确定性事件和可测试性。

适合借鉴：

- 事件模型；
- 策略状态机；
- entry fill -> protection order -> protected position 的明确状态；
- 订单和成交事件幂等。

不直接迁移：改造成本大，当前系统仍需保持最小变更和独立服务。

### Jesse

已确认机制：

- 独立 Position 对象；
- Position risk/liquidation 信息；
- 大量 TP/SL 同时提交、数量相等/小于/大于持仓的回归测试；
- 止损方向和数量约束测试。

适合借鉴：

- 单方向 position contract；
- TP/SL 数量必须不超过真实持仓；
- 保护订单边界的失败测试。

不直接迁移：不是当前 OKX 永续执行路径，也不应把 Jesse 的策略/账户状态混入双向系统。

### OKX Agent Trade Kit

它的仓库主要是 OKX 原生 Grid/DCA bot 管理和 CLI/MCP 工具，不能当作单方向趋势引擎。可参考：

- OKX 认证和模式边界；
- API 工具与交易逻辑分离；
- 实盘/模拟模式显式区分。

不能作为本项目的策略底座，也不能把它的 Grid/DCA 逻辑带入 one_way。

## 对当前 one_way 架构的决策

保留独立模块：

```text
one_way/market       借鉴 Freqtrade Pairlist + Hummingbot OKX data connector
one_way/signal       保留当前 candidate_scoring，独立多空评分
one_way/risk         借鉴 Freqtrade protections + Jesse Position contract
one_way/execution    借鉴 Hummingbot order tracker + Nautilus event separation
one_way/protection   借鉴 Freqtrade exchange stoploss + Nautilus protection tests
one_way/shadow       借鉴 Freqtrade backtest/protection replay
one_way/portfolio    只接当前账户级硬风险适配器
```

## 不可改变的隔离规则

- 不把 Hummingbot/Freqtrade/Jesse/Nautilus 整个引擎接入生产；
- 不把第三方项目的配置直接套到双向系统；
- 不让 one_way 写当前 `data/trading.db`；
- 不让 one_way 进入 `core/engine.py` 的 `FuturesHedgeStrategy` loader；
- 不把 one_way 订单标记成 `normal_grid` 或 `inventory_repair`；
- 不允许第三方策略绕过账户级硬风险；
- 第三方代码只在许可证、接口、服务器 shadow 测试均通过后，才考虑提取机制。

## 下一步

先从 Hummingbot/Freqtrade 的机制中提取设计，不下载整仓：

1. 完善 one_way 独立 universe/market adapter 接口；
2. 定义独立 shadow state schema；
3. 实现 one_way 订单/成交/保护状态机；
4. 在服务器以独立 shadow service 运行；
5. 用真实 OKX 行情验证数据新鲜度、全市场筛选和影子信号；
6. 只有架构隔离、保护覆盖和回放通过后，才评估执行适配器。
