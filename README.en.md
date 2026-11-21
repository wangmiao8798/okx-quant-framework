# OKX Perpetual Swap Hedge-Grid Framework

**English** | [简体中文](README.md)

> A production-grade engineering framework for hedged grid trading on OKX perpetual swaps.
> The focus is order-state correctness, risk gating, and self-healing under real exchange conditions — not on promising returns.

This is not a "profitable strategy in a box". It is a trading framework that has survived real money and real exchange misbehaviour: it rebuilds lost take-profit coverage on its own, knows which direction to allow when the account is pinned against a margin limit, and can stop itself from dismantling the hedge during a one-way trend.

---

## Read this first

- **Nothing here is investment advice. No returns are promised.** Grid strategies earn small in ranging markets and take large losses in trending ones. That is a property of the payoff structure, not a bug to be fixed.
- **The author ran this live and stopped it in November 2026 to reallocate capital to another account.** Before stopping, it took a significant drawdown in a one-way market (unrealised loss exceeded equity at the time) — but with no liquidation, no forced close, and full TP coverage. The figures below are a limited sample, included to show engineering capability and failure boundaries — **not as a track record**.
- Defaults ship as **demo trading** (`flag: '1'`) with `ai_trader` disabled. **Before switching to live, recompute every risk parameter for your own account size** (see "Parameter consistency").
- Leveraged trading can wipe out your entire capital. Run demo for at least one full market cycle first.

---

## What this is / is not

**It is:**
- A runnable OKX V5 perpetual swap system (multiple symbol profiles in one process)
- A complete **order-state consistency** solution: TP coverage reconciliation, phantom-layer detection, runtime self-healing
- An **account-level risk gate**: margin hard/soft limits, net exposure caps, dominant-side budgets, one-sidedness control
- A **self-validating backtest harness** (same strategy logic over historical 1h candles, including ADX trend hit-rate statistics)
- Operable infrastructure: CommandBus control channel, risk sentinel, daily/weekly reports, systemd units

**It is not:**
- High-frequency or low-latency (polling, second-level)
- A multi-tenant SaaS (schema skeleton exists; no tenant isolation, not commercial-ready)
- A turnkey money maker (defaults only fit a specific account size and symbol set)

---

## Core design

### Hedged grid: two legs, layered independently

Each symbol maintains a long leg and a short leg simultaneously, each adding layers at fixed price steps, each layer taking profit independently:

```
        price
          ↑
   sell ──┤  short leg: TP per layer (earns as price falls)
          │
  ────────┼─────── last
          │
   buy ───┤  long leg: TP per layer (earns as price rises)
          ↓
```

- Layer notional = `base_position_usdt × leverage`, scaled by `add_step` and `add_multiplier`
- Every layer places its own `reduce_only` TP order; TP price interpolates from `first_tp_ratio` to `final_tp_ratio`
- As price moves, the corresponding leg keeps adding while the other keeps taking profit — in theory net exposure converges
- **In practice this breaks**: in a one-way trend the counter-trend leg is fully taken-profit out and never refilled, so the book drifts one-sided (see "Failure boundaries")

### TP coverage and phantom-layer self-healing (one of the most valuable parts)

The most lethal live-trading problem is not strategy logic — it is **local state disagreeing with exchange state**. The typical failure chain:

```
local record says layer N has a TP order → exchange has no such order (filled / cancelled / lost on restart)
        ↓
covered quantity is overstated → available_qty is permanently 0
        ↓
every re-place attempt fails with qty_below_min
        ↓
that side never gets TP coverage back → runs naked, exposure silently grows
```

How it is handled:
- `_tp_covered_other_qty()` **only counts layers that carry an order id**, never trusting local quantity records; it floors the result with the exchange view and fails closed when that view is unavailable
- `_heal_missing_tp_coverage()` runs on an independent 120s timer: it pulls live orders from the exchange to break stale caches, and rebuilds coverage for any side that has a position but ~zero live TP
- The rehang path passes `exclude_qty` so the layer currently being replaced is not counted as already covered by itself

> Measured: after manually cancelling every TP order on one side, full coverage was restored within 120 seconds with no restart.

### Account-level risk gate

| Mechanism | Effect |
|---|---|
| `account_margin_hard_limit` | Frozen-margin hard line; above it, all adds are rejected |
| `account_margin_soft_limit` | Soft line; above it the system degrades to reduce-only |
| `net_reducing_hard_margin_exempt` | **Exempts orders that move net exposure toward zero** (see below) |
| `max_net_notional_usdt` | Net exposure cap (the real risk measure) |
| `throttle_dominant_*` | Dominant-side add throttle + net-exposure budget gate |
| `total_loss_guard` / `black_swan` | Total-loss and extreme-market circuit breakers (off by default; calibrate yourself) |
| `liquidation_guard` | Tiered liquidation-distance monitoring |

### Curing the one-sidedness deadlock: the net_reducing exemption

This is the most counter-intuitive design in the framework.

**The deadlock** — a short layer takes profit and closes; the grid wants to re-add that short, but frozen margin is already at the hard line, so the add is rejected indiscriminately → the short side can only shrink → the book becomes purely long → longs consume all margin → frozen margin pushes even harder against the line → re-adding shorts is now impossible. Chicken and egg.

**The fix** — compute `net_before` / `net_after` before placing. If `|net_after| < |net_before|` (the order moves net exposure toward zero, i.e. it is a hedging-leg order), exempt it from the hard line:

```python
net_reducing = abs(net_after) < abs(net_before)
if net_reducing and self._net_reducing_exempt_enabled:
    # allow: this direction reduces risk, it does not add risk
```

**Over-hedging protection** — the exemption only holds inside the "converging toward zero" region. Once an add crosses the perfect-hedge point (net exposure turns negative), `net_reducing` becomes false automatically, so the grid can move net exposure to ≈0 at most — never from "curing a deadlock" into "creating a reverse naked short".

The feature is **opt-in** (`false` by default, fail-closed).

### MOCA market-state classifier (observe-only by default)

`core/moca_engine.py` is a lightweight four-state classifier: `calm` / `trending_up` / `trending_down` / `spike`.

- Called by the engine every 30s, **default `apply_live: false` (observes, never trades)**
- Observe mode records state transitions, so you can later evaluate "how often it would have fired"
- Making it live (auto-adjusting leverage, stopping the counter-trend leg) is a material behaviour change and needs separate calibration

### CommandBus: change runtime state without restarting

`control/command_bus.py` uses SQLite (WAL) as a command queue: insert a row with `status='PENDING'` and the engine picks it up.

| Command | Effect |
|---|---|
| `strategy_start` / `strategy_stop` | Start/stop one strategy (note: stop does **not** flatten or cancel) |
| `risk_resume` / `risk_state_set` | Resume or force a risk state |
| `engine_pause` / `engine_resume` | Pause the whole engine loop |

---

## Architecture

```
main.py                      # multi-profile entry (glob config/config_*.yaml)
└── core/engine.py           # main loop: market data → strategy → risk → orders
    ├── core/moca_engine.py  # market-state classifier (observe-only)
    ├── strategies/
    │   ├── futures_hedge.py # strategy orchestrator
    │   └── hedge_kit/       # responsibility-split mixins
    │       ├── tp.py        # TP coverage, rehang, phantom-layer healing (1720 LOC, heaviest)
    │       ├── grid.py      # layered adds and grid range
    │       ├── inventory.py # inventory repair, over-cap unwind
    │       ├── order.py     # order placement/cancel with retries
    │       ├── account.py   # account snapshot, margin accounting
    │       ├── risk.py      # strategy-side risk context
    │       └── state.py     # runtime state persistence
    ├── risk/                # account-level risk (12 modules)
    │   ├── policy.py        # core policy: hard/soft lines, shadow_order dry-run
    │   ├── position_limits.py / portfolio_budget.py / account_budget.py
    │   ├── black_swan.py / liquidation_guard.py / total_loss_guard.py
    │   └── risk_gate.py / risk_manager.py
    ├── control/command_bus.py   # SQLite command channel
    ├── api/okx_client.py        # OKX V5 REST wrapper
    ├── backtest/hedge_grid.py   # backtest harness (same logic + ADX hit rate)
    ├── scripts/                 # ops / research / reporting
    └── web/                     # FastAPI panel (optional)
```

Scale: **~34,000 lines of Python**.

---

## Quick start (demo)

```bash
# 1. dependencies
pip install -r requirements.txt

# 2. configure API keys (interactive on first run; encrypted into data/keys.enc)
cp config/ai_trader.json.example config/ai_trader.json   # optional, disabled by default

# 3. make sure config/config.yaml has exchange.okx.flag = '1' (demo)

# 4. run
./start.sh
# or
python main.py
```

**Run demo first.** Watch three things: whether TP coverage is complete on both sides, whether adds are being throttled for sensible reasons, and whether net exposure converges.

---

## Parameter consistency (the easiest way to shoot yourself)

Risk parameters are not free-form; they are constrained:

```
account_margin_hard_limit ≥ max_net_notional_usdt ÷ leverage ÷ equity
```

**Violate this and the net-exposure cap is decorative** — adds get killed by the margin hard line long before they can approach the cap.

Two semantic traps worth knowing:
- `throttle_dominant_gap_ratio` defaults to `0`, which means "almost always treat one side as dominant, always throttle it"; setting it to `1.0` effectively disables throttling. **The two extremes mean opposite things.**
- **Frozen-margin ratio is not a risk measure.** A purely hedged book is safe at 70–80% frozen; a one-sided book can be dangerous at 40%. The real measure is **effective leverage = net exposure ÷ equity**.

The sample configs assume an account around 1k USDT. Rescale everything for a different size.

---

## Measured results (limited sample, not a performance promise)

Single account, ~1 month, three symbols (CRV / FARTCOIN / RAM):

| Symbol | Realised net PnL | Note |
|---|---|---|
| RAM | +477 U | main contributor |
| CRV | +81 U | 2,691 fills (many small unwinds) |
| FARTCOIN | −196 U | short leg fully taken-profit out during a downtrend, became naked long |
| **Total** | **+363 U** | fees were the dominant cost |

The single most valuable backtest finding (1h × 90 days): **stopping counter-trend adds while a trend is active is the highest-leverage parameter change** — RAM improved from +44 U to +98 U (drawdown 2.2% → 1.6%), CRV from −55 U to −28 U.

ADX trend hit rate (forward-validated, `TREND_ADX=25`):

| Symbol | Regime precision | Counter-trend leg error rate |
|---|---|---|
| CRV | 61.0% | 30.2% |
| FARTCOIN | 65.8% | 34.4% |
| RAM | 66.0% | 32.2% |

**How to read these numbers**: ADX is right only ~60% of the time, but "stop adding against the trend" is right ~70% of the time, and what it prevents is the most lethal class of loss — one-sided book blowout. It is a seatbelt, not an accelerator.

---

## Failure boundaries (an honest record)

In November 2026 this system took a significant drawdown in a one-way market and was then **stopped by choice**, to move capital to another account that was short of margin. It did not blow up: at shutdown there were no liquidations, TP coverage was complete, and self-healing was running normally. But these boundaries are real:

1. **One-way trends dismantle a symmetric grid.** Rallies take the long leg out of TP → naked short; selloffs take the short leg out → naked long. Both directions happened live. The net_reducing exemption fixes "cannot refill"; it cannot fix "price never comes back".
2. **Unrealised loss can exceed equity.** When you absorb a leveraged position with your own capital buffer, a sustained trend breaks both the risk budget and your nerve.
3. **Sample size is inadequate.** One month, one account, one market regime proves nothing about long-run stability.
4. **Fees are the silent killer.** Two-sided hedging plus frequent layering can eat most of the gross edge.

**Conclusion**: mean-reversion grids are not a "can I implement it correctly" problem — they are a "will the market cooperate" problem. This code solves the former. It cannot solve the latter.

---

## Adding symbols

```bash
cp config/config_fartcoin.yaml config/config_sol.yaml
# edit symbol / ctVal-related parameters / risk thresholds
# restart (main.py glob-loads config/config_*.yaml)
```

Note that `ctVal` (contract value) differs per symbol. **Always take notional value from the exchange's `notionalUsd` field — never compute `qty × markPx` yourself.**

---

## Tests

```bash
pytest -q          # covers TP coverage, one-sidedness exemption, command bus, risk allow-paths
```

> Run it yourself for the current result. Config-validation tests for retired symbols will skip — that's expected.

---

## Known limitations

- Polling (second-level); not suitable for HFT
- One process serving multiple strategies, sharing one account's margin — implicit coupling between symbols
- Multi-user skeleton has no tenant isolation and only basic key encryption (**not commercial-ready**)
- Web panel has no HTTPS and passes tokens in URL query strings — internal/tunnelled use only
- No WebSocket market data (`api/ws_client.py` is a stub)

---

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

This project is for study and research only. Crypto perpetual swaps are extremely risky and can cause total loss of capital. The author accepts no liability for losses incurred by using this project. Assess your own risk and comply with the laws of your jurisdiction. **Never run any trading system with money you cannot afford to lose.**
