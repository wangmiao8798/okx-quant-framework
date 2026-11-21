from __future__ import annotations
import json, sqlite3
from pathlib import Path

class OneWayShadowStore:
    """Owns only the one-way shadow database; never opens trading.db."""
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_runs(
            run_id TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL,
            universe_count INTEGER NOT NULL, scanned_count INTEGER NOT NULL,
            failure_count INTEGER NOT NULL, selected_count INTEGER NOT NULL,
            mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shadow_signals(
            signal_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL, direction TEXT NOT NULL, eligible INTEGER NOT NULL,
            score INTEGER NOT NULL, reasons_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
            execution_attempted INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(run_id) REFERENCES shadow_runs(run_id)
        );
        """)
        self.conn.commit()

    def save(self, run_id: str, ts_ms: int, universe_count: int, scanned_count: int, failures: list[dict], decisions: list[dict], selected: set[str]):
        self.conn.execute("INSERT INTO shadow_runs VALUES(?,?,?,?,?,?,?)", (run_id, ts_ms, universe_count, scanned_count, len(failures), len(selected), "read_only_shadow"))
        for item in decisions:
            d = item["decision"]
            self.conn.execute("INSERT OR REPLACE INTO shadow_signals VALUES(?,?,?,?,?,?,?,?,?,?)", (
                d.signal_id, run_id, ts_ms, item["symbol"], d.direction, int(d.eligible), d.score,
                json.dumps(list(d.reasons), ensure_ascii=False), json.dumps(dict(d.metrics), ensure_ascii=False), 0))
        self.conn.commit()

    def close(self):
        self.conn.close()
