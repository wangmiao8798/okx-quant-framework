# DRAM 策略卡（观察版）

## 现有结论
- 目前项目里只有 `DRAM-USDT-SWAP` 的 **standby 观察档**，没有找到正式实盘记录或历史回测结果。
- 前端已把 DRAM 标记为“高波动观察”，明确提示只适合观察或小资金模拟评估。
- 你今天手动“60 开多”属于 **待验证的经验阈值**，当前没有在代码里找到对应的硬编码规则。

## 已知参数
- `base_position_usdt: 4.0`
- `add_step: 0.012`
- `add_multiplier: 1.15`
- `first_tp_ratio: 0.022`
- `final_tp_ratio: 0.032`
- `max_add_layers: 3`
- `max_tp_orders: 12`
- `risk.stop_loss.total_loss_pct: 12`
- `risk.liquidation.safety_margin_rate: 1.5`
- `risk.black_swan.price_jump_threshold: 1.05`

## 需要验证的假设
- “60 开多”是否对应：低位反转、5m/15m/1H 共振、波动收敛后扩张，或某个外部指标阈值。

## 当前建议
- 不要直接把“60 开多”当成稳定规律上线。
- 先用历史 K 线回测：固定阈值 60 开多，并对比 55/60/65 的入场效果。
