"""SQLite command outbox shared by the Web control plane and trading engine."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path


class CommandBus:
    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS control_commands (
                command_id TEXT PRIMARY KEY,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                command_type TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                actor TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_control_commands_status_created
                ON control_commands(status, created_ts);
            """
        )
        self._conn.commit()

    def _rollback_quietly(self):
        try:
            self._conn.rollback()
        except sqlite3.Error:
            pass

    def enqueue(self, command_type: str, target: str = "", payload=None, actor: str = "web", command_id: str = ""):
        command_id = str(command_id or uuid.uuid4().hex)
        now = int(time.time() * 1000)
        payload_json = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO control_commands(
                        command_id,created_ts,updated_ts,command_type,target,payload_json,
                        actor,status,result_json,error
                    ) VALUES(?,?,?,?,?,?,?,'PENDING','{}','')
                    """,
                    (command_id, now, now, str(command_type), str(target), payload_json, str(actor)),
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                self._rollback_quietly()
                raise
        return command_id

    def claim_pending(self, limit: int = 20):
        now = int(time.time() * 1000)
        stale_before = now - 60_000
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    UPDATE control_commands SET status='PENDING',updated_ts=?,error='recovered stale claim'
                    WHERE status='PROCESSING' AND updated_ts<?
                    """,
                    (now, stale_before),
                )
                rows = self._conn.execute(
                    """
                    SELECT command_id,command_type,target,payload_json,actor
                    FROM control_commands WHERE status='PENDING'
                    ORDER BY created_ts LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
                command_ids = [row[0] for row in rows]
                if command_ids:
                    placeholders = ",".join("?" for _ in command_ids)
                    self._conn.execute(
                        f"UPDATE control_commands SET status='PROCESSING',updated_ts=? "
                        f"WHERE command_id IN ({placeholders}) AND status='PENDING'",
                        (now, *command_ids),
                    )
                self._conn.commit()
            except sqlite3.OperationalError:
                # database locked: skip this tick; next loop iteration retries.
                # Claims were never committed, so commands stay PENDING.
                self._rollback_quietly()
                return []
        result = []
        for command_id, command_type, target, payload_json, actor in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError):
                payload = {}
            result.append(
                {
                    "command_id": command_id,
                    "command_type": command_type,
                    "target": target,
                    "payload": payload,
                    "actor": actor,
                }
            )
        return result

    def complete(self, command_id: str, result=None):
        self._finish(command_id, "COMPLETED", result=result)

    def fail(self, command_id: str, error: str):
        self._finish(command_id, "FAILED", error=error)

    def _finish(self, command_id: str, status: str, result=None, error: str = ""):
        now = int(time.time() * 1000)
        result_json = json.dumps(result or {}, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                self._conn.execute(
                    """
                    UPDATE control_commands
                    SET status=?,updated_ts=?,result_json=?,error=?
                    WHERE command_id=? AND status='PROCESSING'
                    """,
                    (status, now, result_json, str(error), str(command_id)),
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                # database locked: leave row as PROCESSING; the stale-claim
                # recovery in claim_pending() requeues it after 60 seconds.
                self._rollback_quietly()

    def close(self):
        with self._lock:
            self._conn.close()
