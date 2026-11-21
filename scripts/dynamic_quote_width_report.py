#!/usr/bin/env python3
"""Read-only dynamic quote width suggestions from live activity and book quality."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

DATA = Path("data")
OUT = DATA / "dynamic_quote_width_report.json"
ACTIVITY = DATA / "order_activity_report_24h.json"
BOOK = DATA / "orderbook_health.json"
STATUS = DATA / "strategies_status.json"
CONFIGS = [Path("config/config.yaml"), Path("config/config_dram.yaml"), Path("config/config_fartcoin.yaml")]


def run_reports() -> None:
    subprocess.run([sys.executable, "scripts/order_activity_report.py", "--hours", "24", "--json-out", str(ACTIVITY)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, "scripts/sample_orderbook_health.py", "--depth", "5", "--out", str(BOOK)], check=True, stdout=subprocess.DEVNULL)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_configs() -> dict:
    rows = {}
    for path in CONFIGS:
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        f = data.get("futures") or {}
        symbol = f.get("symbol")
        if symbol:
            rows[symbol] = {"path": str(path), "futures": f}
    return rows


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def suggest(symbol: str, cfg: dict, act: dict, book: dict, monitor: dict) -> dict:
    add_step = float(cfg.get("add_step") or 0)
    first_tp = float(cfg.get("first_tp_ratio") or cfg.get("first_tp") or 0)
    final_tp = float(cfg.get("final_tp_ratio") or cfg.get("final_tp") or 0)
    fills_per_order = float(act.get("fills_per_active_order") or 0)
    trades = int(act.get("trades") or 0)
    net = float(act.get("net") or 0)
    max_age = float(act.get("active_max_age_sec") or 0)
    quality = float(book.get("quality_score") or 0)
    spread_pct = float(book.get("spread_pct") or 0)
    margin = float(monitor.get("margin_load_pct") or 0)
    upnl = float(monitor.get("total_unrealized_pnl") or 0)
    reasons = []
    action = "hold"
    step_mult = 1.0
    tp_mult = 1.0

    healthy_book = quality >= 65 and spread_pct <= 0.08
    pressure = margin >= 90 or upnl < -8
    low_fill = fills_per_order < 0.18 and max_age >= 180
    active_fill = fills_per_order >= 0.45 or trades >= 10

    if pressure:
        action = "widen_or_hold_under_pressure"
        step_mult = 1.08
        tp_mult = 1.0
        reasons.append("margin_or_float_loss_pressure")
    elif low_fill and healthy_book and net >= 0:
        action = "narrow_slightly"
        step_mult = 0.92
        tp_mult = 0.96
        reasons.append("low_fill_healthy_book")
    elif active_fill and net > 0 and healthy_book:
        action = "hold_or_widen_slightly"
        step_mult = 1.02
        tp_mult = 1.02
        reasons.append("active_profitable_flow")
    elif quality < 55 or spread_pct > 0.1:
        action = "widen_reduce_activity"
        step_mult = 1.12
        tp_mult = 1.04
        reasons.append("weak_or_wide_book")
    else:
        reasons.append("no_clear_edge_to_change")

    min_step = max(0.0008, add_step * 0.75) if add_step > 0 else 0.0008
    max_step = add_step * 1.35 if add_step > 0 else 0.02
    new_step = round(clamp(add_step * step_mult, min_step, max_step), 6) if add_step > 0 else 0
    new_first_tp = round(clamp(first_tp * tp_mult, first_tp * 0.85, first_tp * 1.25), 6) if first_tp > 0 else 0
    new_final_tp = round(max(new_first_tp + 0.002, final_tp * tp_mult), 6) if final_tp > 0 else 0
    return {
        "symbol": symbol,
        "action": action,
        "reasons": reasons,
        "current": {"add_step": add_step, "first_tp_ratio": first_tp, "final_tp_ratio": final_tp},
        "suggested": {"add_step": new_step, "first_tp_ratio": new_first_tp, "final_tp_ratio": new_final_tp},
        "metrics": {"trades_24h": trades, "net_24h": round(net, 4), "fills_per_active_order": fills_per_order, "active_max_age_sec": max_age, "book_quality": quality, "spread_pct": spread_pct, "margin_load_pct": margin, "unrealized": round(upnl, 4)},
        "readonly": True,
    }


def main() -> None:
    run_reports()
    configs = load_configs()
    activity = load_json(ACTIVITY).get("symbols") or {}
    books = load_json(BOOK).get("symbols") or {}
    status_by_symbol = {s.get("symbol"): s for s in (load_json(STATUS).get("strategies") or {}).values()}
    rows = []
    for symbol, row in configs.items():
        state = status_by_symbol.get(symbol, {})
        rows.append(suggest(symbol, row["futures"], activity.get(symbol, {}), books.get(symbol, {}), state.get("monitor") or {}))
    payload = {"readonly": True, "symbols": rows}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Dynamic quote width report (read-only)")
    for row in rows:
        print("{symbol} action={action} step {old}->{new} tp {oldtp}->{newtp} reasons={reasons}".format(
            symbol=row["symbol"], action=row["action"], old=row["current"]["add_step"], new=row["suggested"]["add_step"], oldtp=row["current"]["first_tp_ratio"], newtp=row["suggested"]["first_tp_ratio"], reasons=",".join(row["reasons"])
        ))


if __name__ == "__main__":
    main()
