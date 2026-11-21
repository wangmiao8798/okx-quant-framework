# Profit-First Grid Evolution

Date: 2026-09-26  
Target: `okx-server:/opt/okx_quant`  
Scope: XAU/USDT and ETH/USDT futures hedge profiles

## Decision

The old `0.08%` grid spacing was too fine after exchange costs. Public
one-minute history over 90 days was replayed in rolling seven-day windows.
Trades were modeled with:

- maker fee `0.02%`
- taker fee `0.05%`
- funding buffer `0.01%` per eight hours
- no same-candle entry and exit
- 7-day windows, restarted every 24 hours

The deployed change widens the order grid and take-profit targets. It does not
change leverage, base size, account margin limits, total notional caps, or the
global stop.

## Before And After

| Symbol | Parameters | Positive 7d windows | p25 net | Median net | Median max drawdown |
|---|---|---:|---:|---:|---:|
| XAU | `0.08%` grid / `0.40%` TP | 44.9% | `-0.285U` | `-0.044U` | `0.489U` |
| XAU | `0.35%` grid / `0.70%` TP | 63.1% | `-0.256U` | `+0.148U` | `0.627U` |
| ETH | `0.08%` grid / `0.35%` TP | 29.3% | `-0.819U` | `-0.392U` | `1.037U` |
| ETH | `0.35%` grid / `0.70%` TP | 61.9% | `-0.364U` | `+0.369U` | `1.005U` |

The net values use the current live layer notional in the simulation. They are
not a promise of future profit; the purpose is to reject the demonstrably
worse high-frequency parameter region before increasing capital.

## Deployed Configuration

XAU:

- grid step: `0.35%`
- first TP: `0.50%`
- final TP: `0.70%`
- maximum add layers: `2`

ETH:

- grid step: `0.35%`
- first TP: `0.50%`
- final TP: `0.70%`
- maximum add layers: `3`

The configured first TP is lower than the simulated flat `0.70%` target
because the live engine adds layered residuals at `1.5x` and `2.0x` of the
first target.

## Live Verification

- `okx-quant.service`: active
- `RiskGate`: `ACTIVE`
- XAU: LONG `3`, SHORT `3`, TP coverage complete
- ETH: LONG `0.08`, SHORT `0.04`, TP coverage complete
- ETH rejected one extra long grid add with `net_notional_limit`; this is the
  configured net-exposure guard working as intended
- CRV, DRAM, FARTCOIN remain halted
- `error.log` has no new traceback after deployment

## Rollback

Backups:

`/home/ubuntu/okx_quant_backups/grid_profit_evolution_20260926/`

Contains:

- `config_xau.before.yaml`
- `config_eth.before.yaml`
- `trading.before.db`
- `strategies_status.before.json`

## Next Scaling Rule

Do not increase `base_position_usdt` yet. The candidate improves the rolling
distribution but still has a negative lower quartile. Keep the current small
size until at least 30 forward closed fills and 10 out-of-sample trades show
positive net PnL after fees and funding. Only then scale size in steps while
holding the account margin hard limit at `30%`.

Research tool:

`/opt/okx_quant/scripts/grid_edge_research.py`
