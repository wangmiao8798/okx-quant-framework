"""Read-only strategy research inspired by HKUDS/Vibe-Trading (MIT).

The production engine remains the only order execution path. This module only
turns one strategy's closed-trade journal into a deterministic 70/30 research
report, following Vibe-Trading's out-of-sample and transaction-cost discipline.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


SOURCE = {
    "project": "HKUDS/Vibe-Trading",
    "commit": "7d42de9",
    "license": "MIT",
}


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _load_trades(path: Path, symbol: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                row_symbol = str(row.get("symbol") or "").strip().upper()
                if row_symbol and row_symbol != symbol.strip().upper():
                    continue
                net = _number(row.get("net") if row.get("net") not in (None, "") else row.get("pnl"))
                rows.append({
                    "time": str(row.get("time") or row.get("ts") or ""),
                    "net": net,
                    "fee": _number(row.get("fee")),
                })
    except (OSError, csv.Error):
        return []
    return rows


def _segment(rows: list[dict[str, Any]], baseline_equity: float) -> dict[str, Any]:
    values = [float(row["net"]) for row in rows]
    fees = sum(float(row["fee"]) for row in rows)
    gross_profit = sum(value for value in values if value > 0)
    gross_loss = abs(sum(value for value in values if value < 0))
    cumulative = peak = max_drawdown = 0.0
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    count = len(values)
    return {
        "trades": count,
        "net_pnl": round(sum(values), 6),
        "avg_net": round(sum(values) / count, 6) if count else None,
        "win_rate_pct": round(sum(value > 0 for value in values) / count * 100, 2) if count else None,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "fees": round(fees, 6),
        "fee_to_profit_pct": round(fees / gross_profit * 100, 2) if gross_profit > 0 else None,
        "max_drawdown_usdt": round(max_drawdown, 6),
        "max_drawdown_pct": round(max_drawdown / baseline_equity * 100, 4) if baseline_equity > 0 else None,
    }


def build_vibe_research(path: Path, symbol: str, baseline_equity: float = 0.0) -> dict[str, Any]:
    """Build an advisory-only out-of-sample report for one strategy journal."""
    rows = _load_trades(Path(path), symbol)
    count = len(rows)
    if count < 2:
        training, out_of_sample = rows, []
    else:
        split = min(count - 1, max(1, int(count * 0.7)))
        training, out_of_sample = rows[:split], rows[split:]

    train_metrics = _segment(training, baseline_equity)
    test_metrics = _segment(out_of_sample, baseline_equity)
    if count == 0:
        status = "no_data"
    elif count < 30 or len(out_of_sample) < 10:
        status = "collecting"
    elif test_metrics["net_pnl"] > 0 and (
        test_metrics["profit_factor"] is None or test_metrics["profit_factor"] >= 1.0
    ):
        status = "positive_oos"
    else:
        status = "weak_oos"

    return {
        "available": bool(rows),
        "advisory_only": True,
        "can_execute": False,
        "status": status,
        "split": "70/30 chronological",
        "minimum_evidence": {"total_trades": 30, "out_of_sample_trades": 10},
        "training": train_metrics,
        "out_of_sample": test_metrics,
        "latest_trade_time": rows[-1]["time"] if rows else None,
        "source": SOURCE,
    }
