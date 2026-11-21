#!/usr/bin/env python3
"""Sync runtime trading files into SQLite for analytics views.

This is a sidecar importer. It reads CSV/JSON files produced by the live
strategy and writes existing analytics tables without touching trading logic.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "trading.db"
PNL_CSV = DATA_DIR / "pnl_records.csv"
FEE_PNL_JSON = DATA_DIR / "fee_pnl.json"
STRATEGY_STATUS_JSON = DATA_DIR / "strategies_status.json"


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid json: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"expected object json: {path}")
    return data


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_time_ms(value: str) -> int:
    value = (value or "").strip()
    if not value:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(value, fmt).timestamp() * 1000)
        except ValueError:
            pass
    try:
        parsed = float(value)
    except ValueError as exc:
        raise SystemExit(f"unsupported time format in {PNL_CSV}: {value!r}") from exc
    return int(parsed * 1000 if parsed < 10_000_000_000 else parsed)


def _stable_order_id(row: dict[str, str]) -> str:
    keys = ["time", "symbol", "side", "qty", "entry", "exit", "gross", "fee", "net"]
    payload = "|".join((row.get(key) or "").strip() for key in keys)
    return "pnl_csv:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()[:24]


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, strategy TEXT, side TEXT,
            symbol TEXT, price REAL, quantity REAL,
            cost REAL, pnl REAL, fee REAL,
            order_id TEXT,
            fill_id TEXT,
            pos_side TEXT,
            reduce_only INTEGER NOT NULL DEFAULT 0,
            fill_type TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS equity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, equity REAL,
            usdt REAL, btc REAL, btc_price REAL
        );
        CREATE TABLE IF NOT EXISTS strategy_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, strategy TEXT, state TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
        CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_equity_ts_unique ON equity(ts);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_status_snapshot_unique
            ON strategy_status(ts, strategy);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    migrations = {
        "fill_id": "TEXT",
        "pos_side": "TEXT",
        "reduce_only": "INTEGER NOT NULL DEFAULT 0",
        "fill_type": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in migrations.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {column} {definition}")
    conn.execute("DROP INDEX IF EXISTS idx_trades_order_id_unique")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_fill_id_unique "
        "ON trades(fill_id) WHERE fill_id IS NOT NULL AND fill_id != ''"
    )


def _has_live_equivalent(
    conn: sqlite3.Connection,
    ts: int,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    pnl: float,
    fee: float,
) -> bool:
    """Avoid importing a close CSV row twice after live fill sync is enabled."""
    rows = conn.execute(
        """
        SELECT ts, quantity, price, pnl, fee
        FROM trades
        WHERE symbol=? AND (pos_side=? OR side=?)
          AND ABS(ts-?) <= 2000
          AND reduce_only=1
          AND (fill_id IS NULL OR fill_id NOT LIKE 'pnl_csv:%')
        """,
        (symbol, side, side, ts),
    ).fetchall()
    for row_ts, row_qty, row_price, row_pnl, row_fee in rows:
        # pnl_records*.csv is deliberately rounded to four decimal places,
        # while the live fill ledger keeps exchange precision.
        pnl_tolerance = max(1e-4, max(abs(float(row_pnl or 0.0)), abs(pnl)) * 1e-5)
        fee_tolerance = max(1e-5, max(abs(float(row_fee or 0.0)), abs(fee)) * 1e-3)
        if (
            abs(float(row_qty or 0.0) - qty) <= 1e-8
            and abs(float(row_price or 0.0) - price) <= max(1e-8, abs(price) * 1e-6)
            and abs(float(row_pnl or 0.0) - pnl) <= pnl_tolerance
            and abs(float(row_fee or 0.0) - fee) <= fee_tolerance
        ):
            return True
    return False


def sync_trades(conn: sqlite3.Connection, csv_path: Path = PNL_CSV) -> int:
    if not csv_path.exists():
        return 0
    inserted = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"time", "symbol", "side", "qty", "exit", "net", "fee"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"missing columns in {csv_path}: {sorted(missing)}")
        for row in reader:
            ts = _parse_time_ms(row.get("time", ""))
            symbol = (row.get("symbol") or "").strip()
            side = (row.get("side") or "").strip()
            qty = _float(row.get("qty"))
            price = _float(row.get("exit") or row.get("price"))
            fee = _float(row.get("fee"))
            pnl = _float(row.get("net") or row.get("pnl"))
            cost = abs(qty * price)
            fill_id = _stable_order_id(row)
            if _has_live_equivalent(conn, ts, symbol, side, qty, price, pnl, fee):
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO trades(
                    ts, strategy, side, symbol, price, quantity, cost, pnl, fee,
                    order_id, fill_id, pos_side, reduce_only, fill_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, "futures_hedge", side, symbol, price, qty, cost, pnl, fee,
                    fill_id, fill_id, side, 1, "close",
                ),
            )
            inserted += int(cur.rowcount > 0)
    return inserted


def _primary_strategy(status: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    strategies = status.get("strategies")
    if isinstance(strategies, dict) and strategies:
        for name, state in strategies.items():
            if isinstance(state, dict) and state.get("is_primary_profile"):
                return str(name), state
        name, state = next(iter(strategies.items()))
        return str(name), state if isinstance(state, dict) else {}
    return "futures", {}


def sync_equity(conn: sqlite3.Connection, status: dict[str, Any], fee_pnl: dict[str, Any]) -> int:
    _name, state = _primary_strategy(status)
    monitor = state.get("monitor") if isinstance(state.get("monitor"), dict) else {}
    updated_at = int(_float(status.get("updated_at"), time.time()))
    ts = updated_at * 1000
    equity = _float(monitor.get("cached_equity"), _float(state.get("cached_equity"), _float(fee_pnl.get("real_equity"), 0.0)))
    usdt = _float(monitor.get("cached_avail"), _float(state.get("cached_avail"), 0.0))
    if equity <= 0:
        return 0
    cur = conn.execute(
        "INSERT OR IGNORE INTO equity(ts, equity, usdt, btc, btc_price) VALUES (?, ?, ?, ?, ?)",
        (ts, equity, usdt, 0.0, _float(monitor.get("price"), 0.0)),
    )
    return int(cur.rowcount > 0)


def sync_strategy_status(conn: sqlite3.Connection, status: dict[str, Any]) -> int:
    if not status:
        return 0
    ts = int(_float(status.get("updated_at"), time.time())) * 1000
    strategies = status.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        strategies = {"futures": status}
    inserted = 0
    for name, state in strategies.items():
        if not isinstance(state, dict):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO strategy_status(ts, strategy, state) VALUES (?, ?, ?)",
            (ts, str(name), json.dumps(state, ensure_ascii=False, sort_keys=True)),
        )
        inserted += int(cur.rowcount > 0)
    return inserted


def sync(db_path: Path = DB_PATH) -> dict[str, int]:
    status = _load_json(STRATEGY_STATUS_JSON)
    fee_pnl = _load_json(FEE_PNL_JSON)
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        csv_paths = sorted(DATA_DIR.glob("pnl_records*.csv"))
        result = {
            "trades_inserted": sum(sync_trades(conn, path) for path in csv_paths),
            "trade_files": len(csv_paths),
            "equity_inserted": sync_equity(conn, status, fee_pnl),
            "strategy_status_inserted": sync_strategy_status(conn, status),
        }
        conn.commit()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync OKX runtime trading data into SQLite.")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite database path")
    args = parser.parse_args()
    result = sync(args.db)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
