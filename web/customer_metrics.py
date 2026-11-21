"""Per-customer strategy metrics from an isolated strategy directory."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from web.vibe_research import build_vibe_research


def _number(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _closed_metrics(path: Path, symbol: str) -> dict[str, Any]:
    result = {"closed_trades": 0, "net_closed_pnl": 0.0, "closed_fees": 0.0}
    if not path.is_file():
        return result
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                row_symbol = str(row.get("symbol") or "")
                if row_symbol and row_symbol != symbol:
                    continue
                result["closed_trades"] += 1
                result["net_closed_pnl"] += _number(row.get("net"), _number(row.get("pnl"), 0.0)) or 0.0
                result["closed_fees"] += _number(row.get("fee"), 0.0) or 0.0
    except (OSError, csv.Error):
        return result
    for key in ("net_closed_pnl", "closed_fees"):
        result[key] = round(result[key], 6)
    return result


def _first_json(strategy_dir: Path, prefix: str) -> dict[str, Any]:
    direct = _load_json(strategy_dir / f"{prefix}.json")
    if direct:
        return direct
    for path in sorted(strategy_dir.glob(f"{prefix}_*.json")):
        value = _load_json(path)
        if value:
            return value
    return {}


def _first_csv(strategy_dir: Path, prefix: str) -> Path:
    direct = strategy_dir / f"{prefix}.csv"
    if direct.is_file():
        return direct
    return next(iter(sorted(strategy_dir.glob(f"{prefix}_*.csv"))), direct)


def build_customer_metrics(strategy_dir: Path, symbol: str) -> dict[str, Any]:
    """Return only metrics stored below one customer strategy directory."""
    strategy_dir = Path(strategy_dir)
    pnl = _first_json(strategy_dir, "fee_pnl")
    records_path = _first_csv(strategy_dir, "pnl_records")
    closed = _closed_metrics(records_path, symbol)
    baseline_equity = _number(pnl.get("risk_equity_baseline"), _number(pnl.get("initial_equity"), 0.0)) or 0.0
    research = build_vibe_research(records_path, symbol, baseline_equity)
    available = bool(pnl or closed["closed_trades"])
    if not available:
        return {
            "data_available": False,
            "realized_pnl": None,
            "total_fees": None,
            "funding_pnl": None,
            "equity": None,
            "available_usdt": None,
            "last_update": None,
            "research": research,
            **closed,
        }
    return {
        "data_available": True,
        "realized_pnl": _number(pnl.get("total_realized_pnl")),
        "total_fees": _number(pnl.get("total_fees_paid")),
        "funding_pnl": _number(pnl.get("total_funding")),
        "equity": _number(pnl.get("real_equity"), _number(pnl.get("cached_equity"))),
        "available_usdt": _number(pnl.get("cached_avail")),
        "last_update": pnl.get("last_update"),
        "research": research,
        **closed,
    }
