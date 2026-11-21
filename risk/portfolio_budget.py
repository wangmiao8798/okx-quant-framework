#!/usr/bin/env python3
"""Read-only portfolio budget planner for live hedge profiles."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

DATA = Path("data")
STATUS = DATA / "strategies_status.json"
FEEDBACK = DATA / "parameter_feedback_report.json"
OUT = DATA / "portfolio_budget_plan.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def score_row(state: dict, feedback: dict) -> tuple[float, list[str]]:
    monitor = state.get("monitor") or {}
    margin = float(monitor.get("margin_load_pct") or 0)
    upnl = float(monitor.get("total_unrealized_pnl") or 0)
    net = float(feedback.get("net_24h") or 0)
    quality = float(feedback.get("orderbook_quality") or 0)
    trades = int(feedback.get("trades_24h") or 0)
    capacity_headroom = float(monitor.get("notional_cap_headroom_usdt") or 0)
    headroom_known = "notional_cap_headroom_usdt" in monitor
    freeze_active = bool(monitor.get("floating_loss_freeze_active", False))
    opening_stats = monitor.get("opening_guard_stats") or {}
    top_block = str(opening_stats.get("top_reason") or "")
    block_count = int(opening_stats.get("top_reason_count") or 0)
    reasons: list[str] = []
    score = 0.0
    if net > 0:
        score += min(35.0, net * 20.0)
        reasons.append("positive_realized_net")
    if trades >= 5:
        score += 15.0
        reasons.append("enough_recent_trades")
    if quality >= 70:
        score += 15.0
        reasons.append("healthy_book")
    elif quality < 55 and quality > 0:
        score -= 20.0
        reasons.append("weak_book")
    if margin > 100:
        score -= 35.0
        reasons.append("over_budget_margin")
    elif margin > 85:
        score -= 20.0
        reasons.append("high_margin")
    elif margin < 55:
        score += 10.0
        reasons.append("has_margin_room")
    # Normal hedge inventory can carry unrealized PnL on both sides.  It is not
    # a frequency or budget gate by itself; only an explicit freeze state is.
    if freeze_active:
        score -= 30.0
        reasons.append("floating_loss_freeze_active")
    capacity_blocked = (
        (headroom_known and capacity_headroom <= 0.0)
        or (top_block in {"max_side_notional", "max_total_notional", "account_margin_budget"} and block_count >= 5)
    )
    if capacity_blocked:
        score -= 20.0
        reasons.append("capacity_or_opening_block")
    return round(score, 2), reasons


def plan() -> dict:
    status = load(STATUS)
    feedback_rows = {r.get("symbol"): r for r in (load(FEEDBACK).get("symbols") or [])}
    rows: List[Dict[str, Any]] = []
    for runtime, state in (status.get("strategies") or {}).items():
        symbol = state.get("symbol") or runtime
        fb = feedback_rows.get(symbol, {})
        score, reasons = score_row(state, fb)
        monitor = state.get("monitor") or {}
        margin = float(monitor.get("margin_load_pct") or 0)
        current_ratio_hint = round(max(0.05, margin / 100.0), 4)
        freeze_active = bool(monitor.get("floating_loss_freeze_active", False))
        capacity_headroom = float(monitor.get("notional_cap_headroom_usdt") or 0)
        headroom_known = "notional_cap_headroom_usdt" in monitor
        opening_stats = monitor.get("opening_guard_stats") or {}
        top_block = str(opening_stats.get("top_reason") or "")
        block_count = int(opening_stats.get("top_reason_count") or 0)
        capacity_blocked = (
            (headroom_known and capacity_headroom <= 0.0)
            or (top_block in {"max_side_notional", "max_total_notional", "account_margin_budget"} and block_count >= 5)
        )
        expansion_eligible = (
            score >= 55
            and not freeze_active
            and not capacity_blocked
        )
        if expansion_eligible:
            action = "increase_soft_budget_small"
            target_ratio_hint = min(0.65, current_ratio_hint + 0.05)
        elif score >= 25:
            action = "hold_budget"
            target_ratio_hint = current_ratio_hint
        elif margin > 95:
            action = "freeze_new_budget"
            target_ratio_hint = current_ratio_hint
        else:
            action = "reduce_activity_or_wait"
            target_ratio_hint = max(0.05, current_ratio_hint - 0.03)
        rows.append({
            "runtime": runtime,
            "symbol": symbol,
            "score": score,
            "action": action,
            "reasons": reasons,
            "margin_load_pct": margin,
            "unrealized": round(float(monitor.get("total_unrealized_pnl") or 0), 4),
            "net_24h": fb.get("net_24h", 0),
            "trades_24h": fb.get("trades_24h", 0),
            "orderbook_quality": fb.get("orderbook_quality", 0),
            "target_margin_ratio_hint": round(target_ratio_hint, 4),
            "readonly": True,
        })
    return {"readonly": True, "symbols": sorted(rows, key=lambda r: r["score"], reverse=True)}


if __name__ == "__main__":
    payload = plan()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Portfolio budget plan (read-only)")
    for row in payload["symbols"]:
        print("{symbol} score={score} action={action} margin={margin_load_pct}% net={net_24h} upnl={unrealized} reasons={reasons}".format(
            **{**row, "reasons": ",".join(row["reasons"])}
        ))
