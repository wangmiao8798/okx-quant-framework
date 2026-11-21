# Strategy Family Research

Date: 2026-09-26  
System: `okx-server:/opt/okx_quant`  
Capital assumption: `1000 USDT`

## Data And Costs

- 180 days of one-minute candles for XAU, ETH, CRV and FARTCOIN.
- DRAM had about 141 days because of its later listing.
- Signals were evaluated on five-minute candles; one-minute history was retained
  for execution and volatility calculations.
- Historical funding rates were loaded from OKX.
- Taker fee `0.05%`, maker fee `0.02%`, slippage `0.02%`.
- Transactions were closed at the final bar so open inventory is not ignored.
- Walk-forward windows used 90 days for training and 30 days for testing.

## Acceptance Gate

A candidate must satisfy all of the following:

- positive out-of-sample net PnL
- profit factor greater than `1.2`
- positive rolling-window ratio greater than `60%`
- maximum drawdown below 8% of account equity
- fees below 40% of gross profit
- neighboring or top-ranked parameter sets remain positive

## Results

### Trend Breakout

No symbol passed. The strategy was too sensitive to transaction costs and
produced frequent reversal churn. On the shorter 60/30-day FARTCOIN test it
lost `240.36 USDT` out of sample.

### Trend Pullback

Only FARTCOIN produced a positive test fold:

- trades: `14`
- net PnL: `+18.78 USDT`
- profit factor: `3.48`
- maximum drawdown: `11.10 USDT`
- positive 14-day rolling ratio: `95.8%`

This result did not survive different starting windows:

- 30-day rolling windows: `40.9%` positive
- 60-day rolling windows: `33.3%` positive

It was rejected as a single-regime result.

### Volatility Grid

The tested grid used ATR-scaled spacing, optional trend filtering, maker
entries/targets, taker inventory stops, a 24-hour maximum hold, and a cooldown
after stop or time exit.

| Symbol | Out-of-sample net | Profit factor | Max drawdown | Positive 14d windows | Decision |
|---|---:|---:|---:|---:|---|
| XAU | `0.00` | n/a | `0.00` | `0%` | no training candidate |
| ETH | `-2.54` | `0.967` | `4.10` | `24.6%` | reject |
| CRV | `-1.09` | `1.027` | `6.98` | `50.8%` | reject |
| FARTCOIN | `+8.95` | `1.212` | `4.65` | `85.2%` | borderline |
| DRAM | `0.00` | n/a | `0.00` | `0%` | no training candidate |

FARTCOIN was then tested with the last 30 days sealed from parameter
selection. The most stable parameter set had `66.7%` positive 30-day windows
in the selection period, but the sealed holdout lost `0.55 USDT`. The best
holdout result was only `+0.27 USDT` with `55.6%` positive windows. This does
not satisfy the gate.

## Decision

No new strategy family is approved for live deployment.

- Existing XAU/ETH live experiments remain at their current small size.
- No leverage, risk limit, base size, or notional cap is increased.
- CRV, FARTCOIN, and DRAM remain halted.
- The research scripts are installed but do not submit orders.

## Installed Research Tools

- `/opt/okx_quant/scripts/grid_edge_research.py`
- `/opt/okx_quant/scripts/strategy_family_research.py`
- `/opt/okx_quant/scripts/volatility_grid_research.py`

Research reports:

- `/opt/okx_quant/reports/research/strategy_family_report_180d.json`
- `/opt/okx_quant/reports/research/volatility_grid_report_180d.json`
- `/opt/okx_quant/reports/research/fart_trend_report_60_30.json`

## Next Research Requirement

The next iteration may not be a parameter tweak. It must add a new source of
edge or materially better conditioning, for example:

- volatility-percentile entry suppression
- regime ensemble rather than a single parameter set
- portfolio allocation across independent strategies
- higher-resolution execution data and order-book spread modeling

The strategies remain in paper/research mode until the full acceptance gate is
met on a new time period.
