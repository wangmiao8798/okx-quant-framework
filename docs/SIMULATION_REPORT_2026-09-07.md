# 云端模拟运行报告（2026-09-07）

## 执行范围

- 执行位置：云服务器 `okx-server`，不是本机。
- 实盘交易服务：`/opt/okx_quant`。
- Directional Shadow/Paper：`/opt/okx_directional`。
- 本轮未恢复交易、未提交真实订单、未修改实盘交易行为。

## 1. DRAM 历史回放

命令使用 `data/dram_5m_recent.csv`（300 根 5 分钟 K 线）执行阈值搜索，原始结果保存于：

`/opt/okx_quant/reports/simulations/dram_threshold_20260907T022850Z.txt`

最佳组合集中在阈值 60、止盈 4%；每个最佳组合仅产生 1 笔交易，胜率 100%，收益约 4.0434%。由于样本短且时间不连续，这只是 DRAM 阈值敏感性分析，不能作为三套多空双开策略的完整回测结论，也不能据此调整实盘参数。

## 2. Directional Shadow

- 服务：`okx-directional-shadow.service`
- 结果：`execution_attempted=false`
- 最近运行：20 个标的、40 个决策、0 个失败
- 运行方式：只读公共行情，不访问私有账户，不提交订单
- timer 当前保持启用，下一次计划运行由 systemd 调度

## 3. Directional Paper

- 服务：`okx-directional-paper.service`
- 结果：`execution_attempted=false`
- 最近运行：20 个标的、20 个决策、0 个失败
- 使用独立虚拟账户和 `/opt/okx_directional/data/one_way.db`
- 最近账户快照：权益约 1009.84 USDT，峰值约 1021.15 USDT，当日初始约 1015.39 USDT，无持仓，未 halted
- 历史累计数据库记录（截至本轮检查）：约 34,038 个信号快照、174 笔纸面交易、336 个保护事件；数据库完整性检查为 `ok`

## 4. 数据隔离

本轮检查确认：

- Shadow/Paper 只写 `/opt/okx_directional/data/one_way.db`。
- 实盘数据库为 `/opt/okx_quant/data/trading.db`，本轮模拟后修改时间未变化。
- 未发现把模拟数据写入实盘 `trading.db` 的迹象。

## 5. 当前实盘保护状态

- `okx-quant.service`：`active (running)`，无重启。
- `okx-quant-web.service`：`active (running)`。
- 三个实盘策略继续保持 `hard_stop`，`resume_required=true`。
- 全局 `RiskGate` 保持 `HALTED`；本轮没有执行 resume。

## 6. 需要跟踪的问题

实盘旧日志中存在 `sqlite3.OperationalError: database is locked`，发生在控制命令读取路径；日志文件时间为 2026-08-22，当前服务自 2026-09-07 10:02 启动后未观察到新的同类错误，暂按历史瞬时锁冲突记录，后续继续监控。

## 7. 后续规则

让现有 Shadow/Paper timer 持续观察 3 至 7 天，收集交易次数、失败率、纸面净值、最大回撤和保护触发情况。观察期内不启用高级模块、不改变实盘参数、不解除 `hard_stop` 或 `RiskGate`。只有在获得更长且连续的数据后，才讨论参数验证或模块恢复。
