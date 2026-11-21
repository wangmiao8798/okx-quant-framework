#!/usr/bin/env python3
"""Read-only live-parameter feedback report.

Combines activity, capital efficiency, orderbook quality and live status into
human-readable recommendations. Does not modify config or orders.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DATA_DIR = Path("data")
STATUS_FILE = DATA_DIR / "strategies_status.json"
ACTIVITY_FILE = DATA_DIR / "order_activity_report_24h.json"
EFF_FILE = DATA_DIR / "capital_efficiency_report.json"
BOOK_FILE = DATA_DIR / "orderbook_health.json"
QUOTE_FILE = DATA_DIR / "dynamic_quote_width_report.json"
OUT_FILE = DATA_DIR / "parameter_feedback_report.json"


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> None:
    run([sys.executable, "scripts/order_activity_report.py", "--hours", "24", "--json-out", str(ACTIVITY_FILE)])
    run([sys.executable, "scripts/capital_efficiency_report.py", "--hours", "24", "--json-out", str(EFF_FILE)])
    run([sys.executable, "scripts/sample_orderbook_health.py", "--depth", "5", "--out", str(BOOK_FILE)])
    try:
        run([sys.executable, "scripts/dynamic_quote_width_report.py"])
    except Exception:
        pass

    status = load(STATUS_FILE)
    activity = (load(ACTIVITY_FILE).get("symbols") or {})
    eff_rows = {row.get("symbol"): row for row in (load(EFF_FILE).get("symbols") or [])}
    books = (load(BOOK_FILE).get("symbols") or {})
    quote_rows = {row.get("symbol"): row for row in (load(QUOTE_FILE).get("symbols") or [])}
    rows = []

    for runtime, state in (status.get("strategies") or {}).items():
        symbol = state.get("symbol", runtime)
        monitor = state.get("monitor") or {}
        act = activity.get(symbol, {})
        eff = eff_rows.get(symbol, {})
        book = books.get(symbol, {})
        quote = quote_rows.get(symbol, {})
        # order_activity_report historically called this field ``fills``;
        # accept both names so feedback never silently treats a busy symbol
        # as having zero trades.
        trades = int(act.get("trades", act.get("fills", 0)) or 0)
        net = float(act.get("net", 0) or 0)
        upnl = float(monitor.get("total_unrealized_pnl", 0) or 0)
        margin = float(monitor.get("margin_load_pct", 0) or 0)
        quality = float(book.get("quality_score", 0) or 0)
        fills_per_order = float(act.get("fills_per_active_order", 0) or 0)
        suggestion = "hold"
        reasons = []
        profit_efficiency = net / max(margin, 1.0)
        severe_pressure = margin >= 98 and upnl < -abs(net) * 10
        high_pressure = margin >= 90 and upnl < -abs(net) * 5
        if quality < 55:
            suggestion = "widen_or_reduce_size"
            reasons.append("weak_orderbook_quality")
        elif trades >= 5 and net > 0 and quality >= 70:
            if severe_pressure:
                suggestion = "hold_no_new_budget"
                reasons.append("severe_margin_and_float_loss_pressure")
            elif high_pressure:
                suggestion = "controlled_expansion_only"
                reasons.append("profitable_but_high_margin_pressure")
            elif profit_efficiency > 0.04:
                suggestion = "eligible_for_more_budget"
                reasons.append("profitable_high_efficiency")
            else:
                suggestion = "eligible_for_small_expansion"
                reasons.append("positive_net_good_book")
        elif trades == 0 and fills_per_order == 0 and quality >= 70:
            suggestion = "consider_narrower_quotes_or_more_triggers"
            reasons.append("healthy_book_no_fills")
        if net > 0 and upnl < -abs(net) * 3:
            reasons.append("realized_profit_with_large_floating_loss")
        row = {
            "runtime": runtime,
            "symbol": symbol,
            "suggestion": suggestion,
            "reasons": reasons,
            "trades_24h": trades,
            "net_24h": round(net, 4),
            "unrealized": round(upnl, 4),
            "margin_load_pct": margin,
            "orderbook_quality": quality,
            "fills_per_active_order": fills_per_order,
            "capital_efficiency_suggestion": eff.get("suggestion", ""),
            "quote_width_action": quote.get("action", ""),
            "quote_width_suggested": quote.get("suggested", {}),
        }
        rows.append(row)

    payload = {"symbols": rows}
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Parameter feedback report")
    for row in rows:
        print("{} suggestion={} reasons={} net={} upnl={} margin={} quality={}".format(
            row["symbol"], row["suggestion"], ",".join(row["reasons"]), row["net_24h"], row["unrealized"], row["margin_load_pct"], row["orderbook_quality"]
        ))


if __name__ == "__main__":
    main()
