# Soft Net Inventory Control

Date: 2026-09-27  
System: `okx-server:/opt/okx_quant`

## Problem

The previous `max_net_notional_usdt` was a hard one-step veto. Once net
exposure exceeded the cap, the dominant side could no longer replenish its
grid. XAU and ETH used a `20 USDT` cap, so a normal inventory imbalance caused
the two-sided grid to degrade into a one-sided grid.

## Professional Change

The hard net limit remains the final risk boundary, but inventory management
now has a soft band:

- below the soft limit: both sides quote normally
- between soft and hard: the dominant side is scaled down progressively
- at or above the hard limit: dominant-side additions stop
- the weak side remains available to reduce net exposure
- reduce-only exits are never restricted

## Configuration

XAU:

- soft net limit: `40 USDT`
- hard net limit: `80 USDT`
- side cap: `100 USDT`
- total cap: `200 USDT`

ETH:

- soft net limit: `40 USDT`
- hard net limit: `80 USDT`
- side cap: `100 USDT`
- total cap: `200 USDT`

## Code Changes

`strategies/hedge_calculations.py`:

- added `inventory_net_size_multiplier()`

`strategies/futures_hedge.py`:

- loads `max_net_notional_soft_usdt`
- calculates net notional using contract value
- scales dominant-side grid and add quantities between soft and hard limits
- blocks dominant-side additions only at the hard limit

`strategies/hedge_status.py`:

- corrected long/short/net notional display to include contract value
- exposes soft limit, hard limit and long/short inventory multipliers

`config/config_xau.yaml` and `config/config_eth.yaml`:

- added the soft limit
- raised the hard net limit from `20 USDT` to `80 USDT`

## Verification

- Full test suite: `233 passed`
- ETH before restart: short grid was blocked
- ETH after restart: LONG grid `2`, SHORT grid `2`
- ETH actual net short notional: about `37.77 USDT`
- ETH soft/hard limits: `40/80 USDT`
- ETH dominant-side multiplier: `1.0` because net exposure is below the soft
  line
- `RiskGate`: `ACTIVE`

## Rollback

Backup directory:

`/home/ubuntu/okx_quant_backups/soft_net_limits_20260927/`

It contains the pre-change versions of:

- `hedge_calculations.before.py`
- `futures_hedge.before.py`
- `hedge_status.before.py`
- `config_xau.before.yaml`
- `config_eth.before.yaml`
- `test_trading_hygiene.before.py`
