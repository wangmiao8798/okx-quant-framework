#!/usr/bin/env python3
"""Read-only profitability and strategy health report for the OKX quant stack."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def fmt(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}"


def summarize_rows(rows):
    summary = defaultdict(lambda: {"n": 0, "gross": 0.0, "fee": 0.0, "net": 0.0, "wins": 0, "profit": 0.0, "loss": 0.0})
    for row in rows:
        symbol = row.get("symbol") or "unknown"
        side = row.get("side") or "unknown"
        gross = float(row.get("gross") or row.get("pnl") or 0)
        fee = float(row.get("fee") or 0)
        net = float(row.get("net") or (gross - fee) or 0)
        item = summary[(symbol, side)]
        item["n"] += 1
        item["gross"] += gross
        item["fee"] += fee
        item["net"] += net
        if net > 0:
            item["wins"] += 1
            item["profit"] += net
        elif net < 0:
            item["loss"] += -net
    return summary


def print_summary(title, summary):
    print(title)
    if not summary:
        print("  no trades")
        return
    total = {"n": 0, "gross": 0.0, "fee": 0.0, "net": 0.0, "wins": 0, "profit": 0.0, "loss": 0.0}
    for item in summary.values():
        for key in total:
            total[key] += item[key]
    rows = [("TOTAL", "all", total)] + [(symbol, side, item) for (symbol, side), item in sorted(summary.items(), key=lambda x: x[1]["net"], reverse=True)]
    for symbol, side, item in rows:
        win_rate = item["wins"] / item["n"] * 100 if item["n"] else 0.0
        pf = item["profit"] / item["loss"] if item["loss"] else None
        pf_text = "∞" if pf is None and item["profit"] > 0 else ("-" if pf is None else fmt(pf, 2))
        fee_ratio = item["fee"] / abs(item["gross"]) * 100 if item["gross"] else 0.0
        print(f"  {symbol:18s} {side:6s} n={item['n']:4d} net={fmt(item['net'])} gross={fmt(item['gross'])} fee={fmt(item['fee'])} win={fmt(win_rate,1)}% pf={pf_text} fee/gross={fmt(fee_ratio,1)}%")


def sqlite_trade_rows(window_hours: int | None):
    db = DATA / "trading.db"
    if not db.exists():
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    params = []
    where = ""
    if window_hours:
        cutoff_ms = int((time.time() - window_hours * 3600) * 1000)
        where = "where ts >= ?"
        params.append(cutoff_ms)
    rows = [dict(r) for r in cur.execute(f"select ts, symbol, side, pnl as gross, fee, pnl-fee as net from trades {where}", params)]
    con.close()
    return rows


def csv_trade_rows(path: Path, window_hours: int | None):
    if not path.exists():
        return []
    cutoff = time.time() - window_hours * 3600 if window_hours else None
    rows = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if cutoff:
                try:
                    row_ts = dt.datetime.strptime(row.get("time", ""), "%Y-%m-%d %H:%M:%S").timestamp()
                    if row_ts < cutoff:
                        continue
                except Exception:
                    pass
            rows.append(row)
    return rows


def equity_window(window_hours: int):
    db = DATA / "trading.db"
    if not db.exists():
        return None
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cutoff_ms = int((time.time() - window_hours * 3600) * 1000)
    start = cur.execute("select ts,equity,usdt from equity where ts >= ? order by ts asc limit 1", (cutoff_ms,)).fetchone()
    end = cur.execute("select ts,equity,usdt from equity order by ts desc limit 1").fetchone()
    con.close()
    if not start or not end:
        return None
    return dict(start=dict(start), end=dict(end))


def load_json(path: Path):
    try:
        stat = path.stat()
        return json.loads(path.read_text()), time.time() - stat.st_mtime
    except Exception:
        return None, None


def report(window_hours: int):
    print(f"OKX Profit Health Report ({window_hours}h window)")
    print("generated_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print()
    for hours in [6, 12, 24, 48, 72]:
        ew = equity_window(hours)
        if not ew:
            continue
        start, end = ew["start"], ew["end"]
        print(f"Equity {hours:>2}h: {fmt(start['equity'],2)} -> {fmt(end['equity'],2)} delta={fmt(end['equity']-start['equity'],2)} USDT {fmt(start['usdt'],2)} -> {fmt(end['usdt'],2)}")
    print()
    print_summary("CRV / primary SQLite realized PnL", summarize_rows(sqlite_trade_rows(window_hours)))
    print()
    fart_rows = csv_trade_rows(DATA / "pnl_records_fartcoin.csv", window_hours)
    print_summary("FARTCOIN sidecar CSV realized PnL", summarize_rows(fart_rows))
    print()
    combined_rows = csv_trade_rows(DATA / "pnl_records.csv", window_hours) + fart_rows
    print_summary("Combined CSV realized PnL", summarize_rows(combined_rows))
    print()
    status, age = load_json(DATA / "strategies_status.json")
    if status:
        print(f"Runtime status age={fmt(age,1)}s running={status.get('running')} halted={status.get('halted_count')}")
        for name, item in (status.get("strategies") or {}).items():
            monitor = item.get("monitor") or {}
            print(f"  {name}: {item.get('symbol')} running={item.get('running')} daily_pnl={item.get('daily_pnl')} session_pnl={item.get('session_pnl')} fees={item.get('total_fees')} margin={monitor.get('margin_load_pct')}% imbalance={monitor.get('imbalance_pct')}% long_add_block={monitor.get('long_add_block_reason')} short_add_block={monitor.get('short_add_block_reason')} event={monitor.get('last_event_type')}/{monitor.get('last_event_detail')}")
    print()
    print("Observations:")
    print("  - Read-only report: no orders, configs, services, or exchange state are changed.")
    print("  - If CRV short stays negative while long is positive, review short-side filters before increasing size.")
    print("  - If fee/gross is high or net is near zero, consider TP/step review only as a separate controlled change.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-hours", type=int, default=24)
    args = parser.parse_args()
    report(args.window_hours)


if __name__ == "__main__":
    main()
