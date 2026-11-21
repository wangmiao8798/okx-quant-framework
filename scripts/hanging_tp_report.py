#!/usr/bin/env python3
"""Read-only report for reduce-only TP orders close to current book/last price."""
from __future__ import annotations

import json
import time
from pathlib import Path

STATUS = Path("data/strategies_status.json")
BOOK = Path("data/orderbook_health.json")
OUT = Path("data/hanging_tp_report.json")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> None:
    status = load(STATUS)
    book = load(BOOK).get("symbols", {})
    rows = []
    for runtime, state in (status.get("strategies") or {}).items():
        symbol = state.get("symbol") or runtime
        monitor = state.get("monitor") or {}
        ob = book.get(symbol, {}) if isinstance(book, dict) else {}
        bid = float(ob.get("bid") or 0)
        ask = float(ob.get("ask") or 0)
        mid = float(ob.get("mid") or monitor.get("price") or 0)
        order_tracker = monitor.get("order_tracker") or {}
        tp_count = int(order_tracker.get("tp_count") or monitor.get("tp_order_count") or 0)
        max_age = float(order_tracker.get("tp_max_age_sec") or monitor.get("tp_order_max_age_sec") or 0)
        rows.append({
            "runtime": runtime,
            "symbol": symbol,
            "tp_count": tp_count,
            "tp_max_age_sec": round(max_age, 1),
            "book_bid": bid,
            "book_ask": ask,
            "book_mid": mid,
            "book_quality": ob.get("quality_score", 0),
            "recommendation": "keep_reduce_only_tp_wide_tolerance" if tp_count > 0 else "no_tp_orders",
            "readonly": True,
        })
    payload = {"generated_at": int(time.time()), "readonly": True, "symbols": rows}
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Hanging TP report (read-only)")
    for row in rows:
        print("{symbol} tp={tp_count} max_age={tp_max_age_sec}s quality={book_quality} recommendation={recommendation}".format(**row))


if __name__ == "__main__":
    main()
