# OKX Quant 商用硬化路线

本文件只记录商用化安全/风控路线，不代表策略已经被真实数据证明稳定盈利。

## 已开始

- `scripts/commercial_risk_report.py`
  - 只读读取 `data/trading.db`、`data/strategies_status.json`、`data/pnl_records*.csv`。
  - 输出权益窗口、最大回撤、已实现 PnL、手续费、当前未实现 PnL、单边偏离、保证金占用、商用门槛状态。
  - 不调用 OKX、不下单、不撤单、不改配置、不重启交易服务。
- `GET /api/commercial/risk-report`
  - Web API 只读报告入口。
  - 复用现有登录校验。

## 下一步优先级

1. Web 安全
   - token 从 URL 查询参数迁移到 Authorization header / httpOnly cookie。
   - 日志脱敏，禁止访问日志记录完整 token。
   - Nginx/Caddy 反代 HTTPS，后端 uvicorn 只监听 localhost。

2. 客户 API Key 安全
   - 每个客户独立加密密钥文件或密钥记录。
   - API Key 权限预检：只允许必要交易权限，不允许提现权限。
   - 新客户强制 paper mode 或小额度试运行。

3. 客户隔离
   - 每个客户绑定 user_id/workspace_id。
   - 策略、订单、报表、API Key、日志按 workspace 隔离。
   - 禁止客户访问全局主策略运行态。

4. 风控闭环
   - 异常熔断条件先只读报警，再逐步接入人工确认动作。
   - 风控报告拆分：已实现利润、未实现浮亏、手续费、权益变化、最大回撤、最大单边偏离、最大保证金占用。
   - 人工接管方案：停止新增、只保留 TP、手动平衡、紧急停机四档。

## 当前报告解释

- `strategy_stats=partial_ready_read_only`：只读统计已开始，但历史多策略覆盖仍需加强。
- `https_reverse_proxy_no_url_token=pending`：尚未完成 HTTPS/反代/token 迁移。
- `encrypted_customer_keys_precheck_forced_paper=partial_existing_needs_hardening`：已有加密/预检雏形，但还没到商用级客户隔离。
- `workspace_isolation=pending`：客户 workspace 隔离还未完成。
- `pnl_breakdown=started`：收益拆分已开始展示，但还需进入面板 UI。
