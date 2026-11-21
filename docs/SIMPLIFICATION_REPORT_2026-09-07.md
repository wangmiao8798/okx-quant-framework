# Multi-Long/Short System Simplification Report

Date: 2026-09-07
Scope: `/opt/okx_quant` on the cloud server
Review: Backend Architect, followed by production configuration changes

## Executive Decision

The system was simplified at the configuration layer. The trading engine, risk
gate, reconciliation, TP coverage, hard-stop, account-equity, margin, position,
and reduce-only exit paths were retained. No historical database tables or
strategy code paths were deleted.

The production account remains effectively empty (`totalEq` approximately
`0.00025 USDT`), so all three strategies remain latched in `hard_stop` and the
global RiskGate remains `HALTED`. No resume or opening order was issued.

## Features Disabled

The following features are disabled in all three production profiles:

- `paired_add_enabled`
- `paired_average_down_enabled`
- `paired_add_allow_inventory_pause`
- `active_inventory_repair_enabled`
- `dynamic_side_notional_enabled`
- `moca.apply_live`

`margin_first_capacity_enabled` is disabled for DRAM and FARTCOIN. CRV did not
have that mode enabled.

DRAM and FARTCOIN hard-margin repair overrides remain disabled. CRV's explicit
override configuration is retained for compatibility with its existing tests,
but it cannot be used while the account-equity and global hard-stop gates reject
the strategy.

## Features Retained

- Basic two-sided grid behavior
- Layered TP and TP coverage safety
- Hard-stop and manual resume latch
- Minimum opening equity and fresh account-margin checks
- Shared-account margin and notional limits
- Exchange reconciliation
- ExecutionGateway and RiskGate final write gate
- Reduce-only exits and emergency reduction paths
- Startup grid rebuild configuration, subject to hard-stop and risk gates

## Expected Impact

The simplified system has fewer independent opening paths and fewer adaptive
parameter changes. This reduces the chance of paired additions, inventory
repair, dynamic sizing, or MOCA updates combining into an unintended exposure.
Position sizes and behavior are easier to audit and reproduce.

The tradeoff is lower adaptability. The system will no longer automatically
add a balancing leg, repair a one-sided inventory imbalance, expand notional
limits from account conditions, or change live grid/leverage/TP parameters from
MOCA. If a position becomes one-sided after a future resume, TP and reduce-only
controls remain active, but weak-side repair will require manual review or a
separately tested re-enable of inventory repair.

`over_cap_unwind_enabled` remains off. The automatic unwind path should only be
enabled after a dedicated reduce-only test with real exchange position fixtures.

## Validation

- Configuration bundle validation: passed; one pre-existing DRAM warning remains
  (`max_net_notional_usdt` exceeds the two-sided cap).
- Full test suite: `220 passed`.
- Service: `okx-quant.service active (running)`.
- Service restart count after deployment: `0`.
- Runtime status: three strategies hard-stopped; global RiskGate `HALTED`.
- No new entries were written to `logs/error.log` after deployment.

## Rollback

The pre-simplification production configs are stored at:

`/home/ubuntu/okx_quant_backups/pre_simplify_20260907/`

Rollback requires restoring those three YAML files and restarting
`okx-quant.service`. Re-enable one feature at a time only after the account has
valid equity and the strategy has passed paper/shadow verification.

## Deferred Work

The architect recommends a separate change for SQLite single-writer ownership,
report freshness/TTL, removal of the unused legacy balance method, and archival
of high-volume reconciliation rows. Those changes were intentionally excluded
from this configuration-only simplification to avoid mixing database migration
risk with strategy behavior changes.
