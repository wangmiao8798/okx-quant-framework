# ATR Grid And Range Boundaries

Date: 2026-09-27  
System: `okx-server:/opt/okx_quant`

## Objective

Absorb the useful behavior of mature grid systems without copying the
unlimited-inventory behavior of spot grids:

- volatility-scaled grid spacing
- explicit upper and lower price boundaries
- pause the side that would add inventory after a breakout
- preserve reduce-only exits
- preserve side, total, margin and net exposure limits

## ATR Grid

The grid step is now:

```text
effective_step = clamp(ATR ratio * multiplier, min_step, max_step)
```

Configuration:

| Symbol | ATR multiplier | Min step | Max step |
|---|---:|---:|---:|
| XAU | `0.6` | `0.08%` | `0.40%` |
| ETH | `0.6` | `0.15%` | `0.80%` |
| FARTCOIN | `0.6` | `0.20%` | `1.20%` |

Live effective steps after MOCA updated:

- XAU: `0.08%`
- ETH: `0.2065%`
- FARTCOIN: `0.9243%`

## Price Boundaries

| Symbol | Lower | Upper | Breakout action |
|---|---:|---:|---|
| XAU | `4200` | `4400` | pause |
| ETH | `2500` | `2900` | pause |
| FARTCOIN | `0.15` | `0.24` | pause |

At the upper boundary, additional short inventory is blocked. At the lower
boundary, additional long inventory is blocked. Existing positions retain
reduce-only orders and can exit normally.

## Code Changes

`strategies/hedge_calculations.py`:

- added `atr_adaptive_step()`

`strategies/futures_hedge.py`:

- loads ATR and range settings
- applies ATR to `_effective_side_step()`
- adds range-side blocking
- retains soft/hard net inventory control

`strategies/hedge_status.py`:

- exposes ATR ratio, effective steps, ranges and boundary block reasons

## Verification

- Full test suite: `235 passed`
- XAU grid: LONG `2`, SHORT `2`
- ETH grid: LONG `2`, SHORT `2`
- FARTCOIN grid: LONG `3`, SHORT `3`
- Boundary status: no side blocked at current prices
- `RiskGate`: `ACTIVE`

## Rollback

Backup directory:

`/home/ubuntu/okx_quant_backups/grid_range_atr_20260927/`

It contains the pre-change versions of:

- `hedge_calculations.before.py`
- `futures_hedge.before.py`
- `hedge_status.before.py`
- `test_trading_hygiene.before.py`
- `config_xau.before.yaml`
- `config_eth.before.yaml`
- `config_fartcoin.before.yaml`
