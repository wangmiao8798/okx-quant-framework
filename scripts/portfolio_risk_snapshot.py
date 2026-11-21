"""Build a live portfolio snapshot for the read-only risk calculator."""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.portfolio_risk_calculator import calculate_portfolio, symbol_permission

DB = ROOT / "data" / "trading.db"
STATUS = ROOT / "data" / "strategies_status.json"


def build_snapshot() -> dict:
    root = json.loads(STATUS.read_text(encoding="utf-8"))
    active = [s for s in root.get("strategies", {}).values() if s.get("running") and s.get("symbol")]
    if not active:
        raise RuntimeError("no running strategy profiles")
    first = active[0].get("monitor", {})
    positions = []
    con = sqlite3.connect(str(DB))
    try:
        for strategy in active:
            symbol = strategy["symbol"]
            for row in con.execute(
                "select pos_side,quantity,average_price,mark_price,notional,unrealized_pnl,payload_json "
                "from exchange_position_state where symbol=? order by pos_side", (symbol,)
            ):
                try:
                    payload = json.loads(row[6] or "{}")
                except Exception:
                    payload = {}
                positions.append({
                    "symbol": symbol, "pos_side": row[0], "quantity": row[1],
                    "average_price": row[2], "mark_price": row[3], "notional": row[4],
                    "unrealized_pnl": row[5], "imr": payload.get("imr"),
                    "mmr": payload.get("mmr"), "liqPx": payload.get("liqPx"),
                })
    finally:
        con.close()
    return {
        "updated_at": root.get("updated_at"),
        "equity": first.get("cached_equity"),
        "margin_load": float(first.get("account_margin_load_pct") or 0.0) / 100.0,
        "mgn_ratio": first.get("account_mgn_ratio"),
        "available_margin": first.get("cached_avail"),
        "positions": positions,
    }


if __name__ == "__main__":
    snapshot = build_snapshot()
    analysis = calculate_portfolio(snapshot)
    root = json.loads(STATUS.read_text(encoding="utf-8"))
    permissions = {}
    for strategy in root.get("strategies", {}).values():
        if not strategy.get("running") or not strategy.get("symbol"):
            continue
        monitor = strategy.get("monitor", {}) or {}
        symbol = strategy["symbol"]
        permissions[symbol] = symbol_permission(
            analysis["state"],
            monitor.get("long_notional", 0.0),
            monitor.get("short_notional", 0.0),
            bool(monitor.get("tp_long_coverage_complete", True)),
            bool(monitor.get("tp_short_coverage_complete", True)),
        )
    analysis["symbol_permissions"] = permissions
    print(json.dumps({"snapshot": snapshot, "analysis": analysis}, ensure_ascii=False, indent=2))
