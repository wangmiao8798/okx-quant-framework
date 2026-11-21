#!/usr/bin/env python3
"""Summarize live order tracker recovery state from strategies_status.json.

Read-only report: does not call exchange, cancel orders, or modify runtime state.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS = ROOT / "data" / "strategies_status.json"
OUT = ROOT / "data" / "order_state_recovery_report.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def classify(row: dict) -> str:
    lost = int(row.get("lost_orders", 0) or 0)
    cached = int(row.get("cached_orders", 0) or 0)
    active = int(row.get("active_orders", 0) or 0)
    if lost > 0:
        return "lost_needs_attention"
    if cached > 0:
        return "temporarily_missing"
    if active > 0:
        return "healthy"
    return "no_live_orders"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default=str(OUT))
    args = parser.parse_args()
    status = load(STATUS)
    rows = []
    for runtime, state in (status.get("strategies") or {}).items():
        monitor = state.get("monitor") or {}
        tracker = monitor.get("order_tracker") or {}
        row = {
            "runtime": runtime,
            "symbol": state.get("symbol", runtime),
            "active_orders": int(tracker.get("active", 0) or 0),
            "cached_orders": int(tracker.get("cached", 0) or 0),
            "lost_orders": int(tracker.get("lost", 0) or 0),
            "total_tracked": int(tracker.get("total", 0) or 0),
            "tp_count": int(tracker.get("tp_count", 0) or 0),
            "grid_count": int(tracker.get("grid_count", 0) or 0),
            "long_active": int(tracker.get("long_active", 0) or 0),
            "short_active": int(tracker.get("short_active", 0) or 0),
            "active_avg_age_sec": float(tracker.get("active_avg_age_sec", 0) or 0),
            "active_max_age_sec": float(tracker.get("active_max_age_sec", 0) or 0),
            "tp_max_age_sec": float(tracker.get("tp_max_age_sec", 0) or 0),
            "grid_max_age_sec": float(tracker.get("grid_max_age_sec", 0) or 0),
            "snapshot_updated_at": status.get("updated_at", 0),
        }
        row["state"] = classify(row)
        rows.append(row)
    payload = {"generated_at": int(time.time()), "status_age_sec": round(time.time() - float(status.get("updated_at", 0) or 0), 1), "symbols": rows}
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Order state recovery report")
    for row in rows:
        print("{symbol} state={state} active={active_orders} cached={cached_orders} lost={lost_orders} tp={tp_count} grid={grid_count}".format(**row))
    print(f"json={out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")


if __name__ == "__main__":
    main()
