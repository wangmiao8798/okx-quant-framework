# Risk Data Separation

Date: 2026-09-29  
System: `okx-server:/opt/okx_quant`

## Problem

OKX returned `50013 Systems are busy`. Account equity could not be read, so
`TotalLossGuard` treated the missing value as a total-loss breach. The system:

- latched `RiskGate=HALTED`
- reported `global_total_loss`
- attempted emergency close-all
- failed the close because the same API outage was active

The actual loss was below the configured 4% threshold.

## Fixed States

The risk system now distinguishes:

```text
NORMAL
ACCOUNT_DATA_UNAVAILABLE
GLOBAL_TOTAL_LOSS
```

### Account Data Unavailable

- uses a 30-second grace period
- blocks new orders
- does not emergency close
- keeps reduce-only exits and stop-profit orders available
- sets RiskGate reason `account_data_unavailable`
- automatically clears when a valid equity value returns

### Global Total Loss

- requires a valid equity value
- requires actual loss >= configured 4%
- still triggers emergency close
- remains a hard risk stop

## Recovery

When valid account equity returns:

- data-unavailable protection is cleared
- RiskGate is restored to `ACTIVE`
- strategies are enabled again
- strategy-level hard stops remain intact

## Verification

- full suite: `240 passed`
- RiskGate restored to `ACTIVE`
- reconciler ready
- account equity read successfully
- XAU/ETH/FARTCOIN remain enabled
- no emergency close on data recovery

## Rollback

Backup:

`/home/ubuntu/okx_quant_backups/risk_data_separation_20260929/`

Contains previous risk guard, risk manager, engine, base config and trading
database.
