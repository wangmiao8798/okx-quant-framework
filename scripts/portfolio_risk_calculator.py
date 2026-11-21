"""Read-only portfolio risk/deleveraging calculator.

This module never places, cancels, or modifies orders. It converts a live
portfolio snapshot into an auditable risk state and reduce-only candidates.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RiskBands:
    normal: float = 0.55
    reduced: float = 0.60
    repair: float = 0.70
    deleverage: float = 0.73
    hard_stop: float = 0.75
    mgn_ratio_warn: float = 15.0
    mgn_ratio_danger: float = 10.0


def classify_account_state(margin_load: float, mgn_ratio: float, bands: RiskBands = RiskBands()) -> str:
    load = _f(margin_load)
    ratio = _f(mgn_ratio)
    if ratio > 0 and ratio <= bands.mgn_ratio_danger:
        return "HARD_STOP"
    if load >= bands.hard_stop:
        return "HARD_STOP"
    if ratio > 0 and ratio <= bands.mgn_ratio_warn:
        return "DELEVERAGE"
    if load >= bands.deleverage:
        return "DELEVERAGE"
    if load >= bands.repair:
        return "REPAIR"
    if load >= bands.reduced:
        return "REDUCED"
    return "NORMAL"


def required_margin_release(equity: float, current_load: float, target_load: float) -> float:
    return max(0.0, _f(equity) * max(0.0, _f(current_load) - _f(target_load)))


def _dominant_side(long_notional: float, short_notional: float) -> str:
    if _f(long_notional) > _f(short_notional):
        return "long"
    if _f(short_notional) > _f(long_notional):
        return "short"
    return ""


def build_deleveraging_candidates(
    positions: list[dict[str, Any]],
    equity: float,
    margin_load: float,
    target_load: float,
) -> list[dict[str, Any]]:
    """Return dominant-side reduce-only candidates, ranked by margin release.

    The estimate uses exchange-reported IMR per contract when available. It is
    deliberately conservative: it never proposes a weak-side opening order or
    a position quantity above the live quantity.
    """
    needed = required_margin_release(equity, margin_load, target_load)
    grouped: dict[str, dict[str, float]] = {}
    for row in positions:
        symbol = str(row.get("symbol") or "")
        side = str(row.get("pos_side") or "")
        qty = max(0.0, _f(row.get("quantity")))
        notional = max(0.0, _f(row.get("notional")))
        imr = max(0.0, _f(row.get("imr")))
        if not symbol or side not in {"long", "short"} or qty <= 0:
            continue
        item = grouped.setdefault(symbol, {"long": 0.0, "short": 0.0, "long_imr": 0.0, "short_imr": 0.0})
        item[side] += notional
        item[f"{side}_imr"] += imr
    out: list[dict[str, Any]] = []
    for symbol, item in grouped.items():
        dominant = _dominant_side(item["long"], item["short"])
        if not dominant:
            continue
        qty_rows = [r for r in positions if str(r.get("symbol") or "") == symbol and r.get("pos_side") == dominant]
        for row in qty_rows:
            qty = max(0.0, _f(row.get("quantity")))
            imr = max(0.0, _f(row.get("imr")))
            imr_per_qty = imr / qty if qty else 0.0
            if imr_per_qty <= 0:
                continue
            qty_needed = min(qty, needed / imr_per_qty) if needed > 0 else 0.0
            out.append({
                "symbol": symbol,
                "pos_side": dominant,
                "reduce_only_side": "sell" if dominant == "long" else "buy",
                "live_qty": qty,
                "imr": imr,
                "imr_per_qty": imr_per_qty,
                "qty_for_target": qty_needed,
                "estimated_margin_release": qty_needed * imr_per_qty,
                "net_notional_before": item["long"] - item["short"],
                "reason": "dominant_side_margin_release",
            })
    out.sort(key=lambda x: (-x["imr_per_qty"], -abs(x["net_notional_before"]), x["symbol"]))
    remaining = needed
    for item in out:
        item["recommended_qty_if_first"] = min(item["live_qty"], remaining / item["imr_per_qty"] if item["imr_per_qty"] else 0.0)
        item["recommended_release_if_first"] = item["recommended_qty_if_first"] * item["imr_per_qty"]
        remaining = max(0.0, remaining - item["recommended_release_if_first"])
    return out


def symbol_permission(
    account_state: str,
    long_notional: float,
    short_notional: float,
    tp_long_complete: bool = True,
    tp_short_complete: bool = True,
) -> dict[str, Any]:
    """Return per-symbol opening permissions under the account ceiling.

    This is deliberately conservative: account state can reduce permissions,
    but a symbol cannot override HARD_STOP. A missing TP side blocks opening
    that side until coverage is repaired.
    """
    state = str(account_state or "HARD_STOP").upper()
    long_n = max(0.0, _f(long_notional))
    short_n = max(0.0, _f(short_notional))
    if not tp_long_complete or not tp_short_complete:
        return {"mode": "BLOCKED", "allow_normal_grid": False, "allow_repair": False, "reason": "tp_coverage_incomplete"}
    if state == "HARD_STOP":
        return {"mode": "DELEVERAGE_ONLY", "allow_normal_grid": False, "allow_repair": False, "reason": "portfolio_hard_stop"}
    if state == "DELEVERAGE":
        return {"mode": "DELEVERAGE_ONLY", "allow_normal_grid": False, "allow_repair": False, "reason": "portfolio_deleverage"}
    if long_n > short_n * 1.10:
        dominant = "long"
    elif short_n > long_n * 1.10:
        dominant = "short"
    else:
        dominant = ""
    if state == "REPAIR":
        return {"mode": "REPAIR_ONLY", "allow_normal_grid": False, "allow_repair": bool(dominant), "repair_side": "short" if dominant == "long" else "long" if dominant == "short" else "", "reason": "portfolio_repair"}
    if state == "REDUCED":
        return {"mode": "REDUCED", "allow_normal_grid": True, "allow_repair": bool(dominant), "repair_side": "short" if dominant == "long" else "long" if dominant == "short" else "", "reason": "portfolio_reduced"}
    return {"mode": "NORMAL", "allow_normal_grid": True, "allow_repair": bool(dominant), "repair_side": "short" if dominant == "long" else "long" if dominant == "short" else "", "reason": "portfolio_normal"}


def calculate_portfolio(snapshot: dict[str, Any], bands: RiskBands = RiskBands()) -> dict[str, Any]:
    equity = _f(snapshot.get("equity"))
    load = _f(snapshot.get("margin_load"))
    ratio = _f(snapshot.get("mgn_ratio"))
    state = classify_account_state(load, ratio, bands)
    target = bands.repair if state in {"DELEVERAGE", "HARD_STOP"} else bands.reduced
    candidates = build_deleveraging_candidates(snapshot.get("positions", []), equity, load, target)
    return {
        "state": state,
        "equity": equity,
        "margin_load": load,
        "mgn_ratio": ratio,
        "target_load": target,
        "required_margin_release": required_margin_release(equity, load, target),
        "candidates": candidates,
        "read_only": True,
    }


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(calculate_portfolio(json.load(sys.stdin)), ensure_ascii=False, indent=2))
