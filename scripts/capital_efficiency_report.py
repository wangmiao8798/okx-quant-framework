#!/usr/bin/env python3
"""Read-only capital efficiency report per symbol."""
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")
STATUS_FILE = DATA_DIR / "strategies_status.json"
CSV_FILES = [DATA_DIR / "pnl_records.csv", DATA_DIR / "pnl_records_dram.csv", DATA_DIR / "pnl_records_fartcoin.csv"]


def parse_time(raw: str) -> float:
    raw = str(raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).timestamp()
        except ValueError:
            pass
    try:
        value = float(raw)
        return value / 1000 if value > 10_000_000_000 else value
    except Exception:
        return 0.0


def read_trades(since_ts: float) -> dict:
    out = defaultdict(lambda: {"trades": 0, "net": 0.0, "gross": 0.0, "fee": 0.0})
    for path in CSV_FILES:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ts = parse_time(row.get("time") or row.get("timestamp") or row.get("ts"))
                if ts < since_ts:
                    continue
                symbol = row.get("symbol", "")
                if not symbol:
                    continue
                try:
                    net = float(row.get("net") or row.get("pnl") or 0)
                    gross = float(row.get("gross") or 0)
                    fee = float(row.get("fee") or 0)
                except ValueError:
                    continue
                out[symbol]["trades"] += 1
                out[symbol]["net"] += net
                out[symbol]["gross"] += gross
                out[symbol]["fee"] += fee
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only capital efficiency report")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--json-out", type=Path, default=DATA_DIR / "capital_efficiency_report.json")
    args = parser.parse_args()

    status = json.loads(STATUS_FILE.read_text(encoding="utf-8")) if STATUS_FILE.exists() else {}
    trades = read_trades(time.time() - args.hours * 3600)
    rows = []
    for runtime, state in (status.get("strategies") or {}).items():
        symbol = state.get("symbol", runtime)
        monitor = state.get("monitor") or {}
        stats = trades.get(symbol, {"trades": 0, "net": 0.0, "gross": 0.0, "fee": 0.0})
        equity = float(monitor.get("cached_equity") or state.get("cached_equity") or 0)
        margin_load = float(monitor.get("margin_load_ratio") or 0)
        margin_used = max(0.0, equity * margin_load)
        net = float(stats["net"])
        fee = float(stats["fee"])
        gross = float(stats["gross"])
        fee_to_gross = fee / abs(gross) if abs(gross) > 1e-9 else 0.0
        net_per_margin = net / margin_used if margin_used > 1e-9 else 0.0
        unrealized = float(monitor.get("total_unrealized_pnl") or 0)
        score = net_per_margin * 100 - max(0.0, fee_to_gross - 0.2) * 2 + min(0.0, unrealized / max(margin_used, 1.0))
        row = {
            "runtime": runtime,
            "symbol": symbol,
            "trades": int(stats["trades"]),
            "net": round(net, 4),
            "gross": round(gross, 4),
            "fee": round(fee, 4),
            "fee_to_gross": round(fee_to_gross, 4),
            "equity": round(equity, 2),
            "margin_load_pct": round(margin_load * 100, 2),
            "margin_used_est": round(margin_used, 2),
            "net_per_margin": round(net_per_margin, 5),
            "unrealized": round(unrealized, 4),
            "score": round(score, 5),
            "suggestion": "hold",
        }
        if row["trades"] >= 3 and net_per_margin > 0.005 and unrealized > -margin_used * 0.05:
            row["suggestion"] = "eligible_more_budget"
        elif margin_load > 0.75 and unrealized < 0:
            row["suggestion"] = "avoid_more_budget"
        elif row["trades"] == 0 and margin_load > 0.3:
            row["suggestion"] = "reduce_or_wait"
        rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    # --- Added 2026-10-16: account-level summary ---
    # The per-symbol rows above only count realized PnL inside the window and
    # list unrealized separately, which reads as profitable while floating
    # losses are large. This block rolls everything into one true number.
    total_realized = sum(r["net"] for r in rows)
    total_unrealized = sum(r["unrealized"] for r in rows)
    total_fees = sum(r["fee"] for r in rows)
    equity_now = rows[0]["equity"] if rows else 0.0
    portfolio = {
        "realized_net_window": round(total_realized, 4),
        "unrealized_open": round(total_unrealized, 4),
        "fees_window": round(total_fees, 4),
        "true_net_window": round(total_realized + total_unrealized, 4),
        "equity_now": round(equity_now, 2),
        "note": "true_net_window = realized_net_window + unrealized_open (fees already netted in realized)",
    }
    payload = {"generated_at": int(time.time()), "window_hours": args.hours, "portfolio": portfolio, "symbols": rows}
    args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Capital efficiency window={}h".format(args.hours))
    print("PORTFOLIO realized={} unrealized={} true_net={} equity={}".format(
        portfolio["realized_net_window"], portfolio["unrealized_open"],
        portfolio["true_net_window"], portfolio["equity_now"]))
    for row in rows:
        print("{} trades={} net={} margin={} net/margin={} upnl={} score={} suggestion={}".format(
            row["symbol"], row["trades"], row["net"], row["margin_load_pct"], row["net_per_margin"], row["unrealized"], row["score"], row["suggestion"]
        ))


if __name__ == "__main__":
    main()
