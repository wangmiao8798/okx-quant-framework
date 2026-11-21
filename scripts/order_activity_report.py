#!/usr/bin/env python3
"""Read-only order activity report combining realized CSV trades and live order ages."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path("data")
DB_FILE = DATA_DIR / "trading.db"
STATUS_FILE = DATA_DIR / "strategies_status.json"


def read_db_trades(since_ms: int) -> list[dict]:
    if not DB_FILE.exists():
        return []
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            """
            SELECT ts,symbol,side,quantity,pnl,fee,fill_id,pos_side,fill_type
            FROM trades WHERE ts>=? ORDER BY ts
            """,
            (since_ms,),
        ).fetchall()
    return [
        {
            "ts": int(row[0] or 0), "symbol": row[1] or "", "side": row[2] or "",
            "qty": float(row[3] or 0), "net": float(row[4] or 0),
            "fee": float(row[5] or 0), "fill_id": row[6] or "",
            "pos_side": row[7] or "", "fill_type": row[8] or "",
        }
        for row in rows
    ]


def summarize_trades(trades: list[dict]) -> dict:
    by_symbol = defaultdict(lambda: {"fills": 0, "open_fills": 0, "close_fills": 0, "net": 0.0, "fee": 0.0, "gross": 0.0, "legacy_csv_fills": 0, "live_fills": 0})
    for row in trades:
        item = by_symbol[row["symbol"]]
        item["fills"] += 1
        item["fee"] += row["fee"]
        is_close = row["fill_type"] == "close" or bool(row["pos_side"]) and row["side"] in ("buy", "sell") and row["fill_type"] != "open"
        if is_close:
            item["close_fills"] += 1
            item["net"] += row["net"]
            item["gross"] += row["net"] + row["fee"]
        else:
            item["open_fills"] += 1
            item["net"] -= row["fee"]
        if str(row["fill_id"]).startswith("pnl_csv:"):
            item["legacy_csv_fills"] += 1
        else:
            item["live_fills"] += 1
    for item in by_symbol.values():
        item["close_open_ratio"] = round(item["close_fills"] / max(item["open_fills"], 1), 6)
        # CSV rows are close-only compatibility records. Once they are present,
        # this window cannot claim complete open/close accounting from SQLite.
        item["data_complete"] = item["legacy_csv_fills"] == 0
    return by_symbol


def reject_summary(since_ms: int) -> dict:
    result = defaultdict(lambda: defaultdict(int))
    if not DB_FILE.exists():
        return result
    with sqlite3.connect(DB_FILE) as conn:
        local_rows = conn.execute(
            """
            SELECT i.symbol,e.message FROM order_events e
            JOIN order_intents i ON i.intent_id=e.intent_id
            WHERE e.ts>=? AND e.event_type='LOCAL_REJECT'
            """, (since_ms,)
        ).fetchall()
        for symbol, reason in local_rows:
            result[symbol]["local:" + (reason or "local_reject")] += 1
        api_rows = conn.execute(
            """
            SELECT i.symbol,e.payload_json FROM order_events e
            JOIN order_intents i ON i.intent_id=e.intent_id
            WHERE e.ts>=? AND e.event_type='API_RESULT' AND e.status_code NOT IN ('','0')
            """, (since_ms,)
        ).fetchall()
        for symbol, payload in api_rows:
            reason = "api:unknown"
            try:
                data = (json.loads(payload or "{}").get("data") or [{}])[0]
                reason = "api:" + str(data.get("sCode") or data.get("sMsg") or "unknown")
            except (TypeError, ValueError, IndexError):
                pass
            result[symbol][reason] += 1
        risk_rows = conn.execute(
            "SELECT symbol,reason_code FROM risk_decisions WHERE ts>=? AND allowed=0",
            (since_ms,),
        ).fetchall()
        for symbol, reason in risk_rows:
            result[symbol]["risk:" + (reason or "risk_gate_rejected")] += 1
    return result


def floating_loss_duration(since_ms: int, strategy: str) -> dict:
    if not DB_FILE.exists():
        return {"negative_upl_hours": 0.0, "floating_loss_freeze_hours": 0.0}
    with sqlite3.connect(DB_FILE) as conn:
        rows = conn.execute(
            "SELECT ts,state FROM strategy_status WHERE strategy=? AND ts>=? ORDER BY ts",
            (strategy, since_ms),
        ).fetchall()
    negative_ms = freeze_ms = 0
    now_ms = int(time.time() * 1000)
    for index, (ts, raw) in enumerate(rows):
        try:
            monitor = json.loads(raw or "{}").get("monitor") or {}
            next_ts = rows[index + 1][0] if index + 1 < len(rows) else now_ms
            delta = max(0, min(int(next_ts) - int(ts), 180000))
            if float(monitor.get("total_unrealized_pnl") or 0) < 0:
                negative_ms += delta
            if monitor.get("floating_loss_freeze_active"):
                freeze_ms += delta
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return {"negative_upl_hours": round(negative_ms / 3600000, 4), "floating_loss_freeze_hours": round(freeze_ms / 3600000, 4)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only order activity report")
    parser.add_argument("--hours", type=float, default=24, help="lookback window")
    parser.add_argument("--json-out", type=Path, default=DATA_DIR / "order_activity_report.json")
    args = parser.parse_args()

    since_ms = int((time.time() - args.hours * 3600) * 1000)
    by_symbol = summarize_trades(read_db_trades(since_ms))
    rejects = reject_summary(since_ms)
    status = json.loads(STATUS_FILE.read_text(encoding="utf-8")) if STATUS_FILE.exists() else {}
    payload = {"generated_at": int(time.time()), "window_hours": args.hours, "symbols": {}}

    print("Order activity report window={}h".format(args.hours))
    for runtime, state in (status.get("strategies") or {}).items():
        symbol = state.get("symbol", runtime)
        monitor = state.get("monitor") or {}
        tracker = monitor.get("order_tracker") or {}
        trade_stats = by_symbol.get(symbol, {"fills": 0, "open_fills": 0, "close_fills": 0, "net": 0.0, "fee": 0.0, "gross": 0.0, "close_open_ratio": 0.0, "data_complete": False})
        active = int(tracker.get("active", 0) or 0)
        max_age = float(tracker.get("active_max_age_sec", 0) or 0)
        avg_age = float(tracker.get("active_avg_age_sec", 0) or 0)
        trades_count = int(trade_stats["fills"])
        fills_per_active_order = trades_count / max(active, 1)
        stale_threshold_hours = max(1.0, args.hours * 0.25)
        float_stats = floating_loss_duration(since_ms, runtime)
        row = {
            "runtime": runtime,
            "symbol": symbol,
            "fills": trades_count,
            "trades": trades_count,
            "open_fills": int(trade_stats["open_fills"]),
            "close_fills": int(trade_stats["close_fills"]),
            "close_open_ratio": trade_stats["close_open_ratio"],
            "net": round(float(trade_stats["net"]), 4),
            "fee": round(float(trade_stats["fee"]), 4),
            "gross": round(float(trade_stats["gross"]), 4),
            "active_orders": active,
            "cached_orders": int(tracker.get("cached", 0) or 0),
            "lost_orders": int(tracker.get("lost", 0) or 0),
            "active_avg_age_sec": avg_age,
            "active_max_age_sec": max_age,
            "grid_max_age_sec": float(tracker.get("grid_max_age_sec", 0) or 0),
            "tp_max_age_sec": float(tracker.get("tp_max_age_sec", 0) or 0),
            "fills_per_active_order": round(fills_per_active_order, 4),
            "stale_order_flag": bool(max_age / 3600 >= stale_threshold_hours and trades_count == 0),
            "margin_load_pct": monitor.get("margin_load_pct", 0),
            "block_long": monitor.get("long_add_block_reason", ""),
            "block_short": monitor.get("short_add_block_reason", ""),
            "reject_reasons": dict(rejects.get(symbol, {})),
            "negative_upl_hours": float_stats.get("negative_upl_hours", 0.0),
            "floating_loss_freeze_hours": float_stats.get("floating_loss_freeze_hours", 0.0),
            "data_complete": bool(trade_stats.get("data_complete")),
        }
        payload["symbols"][symbol] = row
        print("{}: fills={} open/close={}/{} net={} fee={} active={} max_age={}s fills/order={} stale={} block={}/{} rejects={}".format(
            symbol, row["fills"], row["open_fills"], row["close_fills"], row["net"], row["fee"],
            active, int(max_age), row["fills_per_active_order"], row["stale_order_flag"], row["block_long"], row["block_short"]
            , row["reject_reasons"]
        ))
    args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
