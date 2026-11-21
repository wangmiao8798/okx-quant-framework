#!/usr/bin/env python3
"""Read-only profitability baseline after a risk-control repair.

The report refuses to run without an explicit repair timestamp. This prevents
legacy incident losses from being mixed into the post-repair strategy sample.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DEFAULT_BASELINE = DATA / "repair_baseline.json"


def number(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return default


def parse_timestamp(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000.0 if value > 10_000_000_000 else value
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return parse_timestamp(float(raw))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(raw, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"unsupported timestamp: {value!r}")


def load_baseline(path: Path, since: str | None) -> tuple[float, str]:
    if since:
        ts = parse_timestamp(since)
        return ts, since
    if not path.exists():
        raise SystemExit(f"missing repair baseline: pass --since or create {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("baseline_at") or payload.get("repair_at")
        ts = parse_timestamp(raw)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid repair baseline {path}: {exc}") from exc
    return ts, str(raw)


def csv_rows(since: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(DATA.glob("pnl_records*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                try:
                    ts = parse_timestamp(row.get("time") or row.get("ts"))
                except ValueError:
                    continue
                if ts < since:
                    continue
                gross = number(row.get("gross") or row.get("pnl"))
                fee = abs(number(row.get("fee")))
                net = number(row.get("net"), gross - fee)
                rows.append({"ts": ts, "symbol": row.get("symbol") or path.stem, "side": row.get("side") or "unknown", "gross": gross, "fee": fee, "net": net, "qty": abs(number(row.get("qty") or row.get("quantity")))})
    return rows


def equity_summary(since: float) -> dict[str, Any]:
    db = DATA / "trading.db"
    if not db.exists():
        return {"available": False, "reason": "trading.db missing"}
    with sqlite3.connect(db) as conn:
        first = conn.execute("select ts,equity,usdt from equity where ts>=? order by ts asc limit 1", (int(since * 1000),)).fetchone()
        last = conn.execute("select ts,equity,usdt from equity order by ts desc limit 1").fetchone()
        count = conn.execute("select count(*) from equity where ts>=?", (int(since * 1000),)).fetchone()[0]
    if not first or not last:
        return {"available": False, "reason": "no equity samples after baseline", "samples": count}
    return {"available": True, "samples": count, "start_equity": first[1], "end_equity": last[1], "equity_delta": last[1] - first[1], "start_available": first[2], "end_available": last[2]}


def build_report(since: float, baseline_label: str) -> dict[str, Any]:
    rows = csv_rows(since)
    gross = sum(row["gross"] for row in rows)
    fees = sum(row["fee"] for row in rows)
    net = sum(row["net"] for row in rows)
    wins = sum(1 for row in rows if row["net"] > 0)
    symbols: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = symbols.setdefault(row["symbol"], {"fills": 0, "gross": 0.0, "fees": 0.0, "net": 0.0, "qty": 0.0, "wins": 0})
        item["fills"] += 1
        item["gross"] += row["gross"]
        item["fees"] += row["fee"]
        item["net"] += row["net"]
        item["qty"] += row["qty"]
        item["wins"] += int(row["net"] > 0)
    for item in symbols.values():
        item["win_rate_pct"] = round(item["wins"] / item["fills"] * 100, 2) if item["fills"] else 0.0
        for key in ("gross", "fees", "net", "qty"):
            item[key] = round(item[key], 8)
    status_path = DATA / "strategies_status.json"
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        status = {}
    current = {}
    for runtime, state in (status.get("strategies") or {}).items():
        monitor = state.get("monitor") or {}
        current[runtime] = {
            "symbol": state.get("symbol", runtime),
            "unrealized_pnl": number(monitor.get("total_unrealized_pnl")),
            "long_qty": number(state.get("long_qty")),
            "short_qty": number(state.get("short_qty")),
            "inventory_recovery_remaining_sec": number(monitor.get("recovery_remaining_sec")),
        }
    funding = {"available": False, "reason": "funding records have no per-event timestamp in fee_pnl.json"}
    return {
        "generated_at": int(time.time()),
        "baseline_at": baseline_label,
        "baseline_epoch": int(since),
        "readonly": True,
        "sample": {"fills": len(rows), "gross_pnl": round(gross, 8), "fees": round(fees, 8), "net_pnl": round(net, 8), "win_rate_pct": round(wins / len(rows) * 100, 2) if rows else 0.0, "symbols": symbols},
        "equity": equity_summary(since),
        "funding": funding,
        "current": current,
        "accounting_notes": ["CSV rows are close/TP fills and do not prove complete open-to-close round trips.", "Funding is intentionally unavailable until timestamped account-bill ingestion is added.", "Unrealized PnL is a current snapshot, not post-repair realized PnL."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a post-repair read-only profit baseline")
    parser.add_argument("--since", help="repair timestamp (epoch, ISO-8601, or YYYY-MM-DD HH:MM:SS)")
    parser.add_argument("--baseline-file", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    since, label = load_baseline(args.baseline_file, args.since)
    report = build_report(since, label)
    output = args.json_out or (DATA / "post_repair_profit_report.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
