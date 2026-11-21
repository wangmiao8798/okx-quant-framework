# FARTCOIN Hard-Limit Hedge

Date: 2026-09-29  
System: `okx-server:/opt/okx_quant`

## Change

FARTCOIN net exposure limits were widened and a hard-limit hedge was added.

| Setting | Value |
|---|---:|
| Soft net exposure | `150 USDT` |
| Hard net exposure | `300 USDT` |
| Hedge order cap | `150 USDT` |
| Confirmation checks | `2` |
| Cooldown | `60 seconds` |

## Behavior

- Below `150 USDT`: both grids run normally.
- From `150` to `300 USDT`: the dominant side is scaled down.
- At or above `300 USDT`: a weak-side hedge is submitted.
- At most `150 USDT` of hedge notional is submitted at one time.
- Additional hedge orders wait for the cooldown.
- Reduce-only exits remain available at all times.

## Implementation

`strategies/hedge_calculations.py`:

- added `hard_hedge_order_qty()`

`strategies/futures_hedge.py`:

- added hard-breach confirmation, cooldown and weak-side hedge execution
- keeps the existing soft-limit dominant-side scaling

`strategies/hedge_status.py`:

- exposes hard-hedge enablement, breach count, cooldown and order cap

## Live Verification

At deployment:

- LONG: `1312`
- SHORT: `0`
- Net long: about `217.5 USDT`
- Soft/hard: `150/300 USDT`
- Dominant-side multiplier: about `0.595`
- LONG grid: `2`
- SHORT grid: `2`
- Hard-hedge breach count: `0`
- `RiskGate`: `ACTIVE`
- Full tests: `236 passed`

## Rollback

Backup:

`/home/ubuntu/okx_quant_backups/fart_hard_hedge_20260929/`

Contains the previous code, tests, FARTCOIN configuration and strategy status.
