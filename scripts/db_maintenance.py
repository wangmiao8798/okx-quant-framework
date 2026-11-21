#!/usr/bin/env python3
"""trading.db maintenance: archive old rows, purge, vacuum.

Usage:
  python3 db_maintenance.py --db /opt/okx_quant/data/trading.db \
      --archive /opt/okx_quant/_archive/trading_archive.db \
      --retention-days 30 [--vacuum] [--dry-run]

Rows older than the retention window are copied into the rolling archive
DB (via ATTACH, idempotent) and then deleted from the live DB.
Tables in KEEP_ALL_TABLES are never touched.
"""
import argparse
import os
import sqlite3
import time
import sys

# table -> time column (milliseconds epoch)
TIME_TABLES = {
    "order_events": "ts",
    "strategy_status": "ts",
    "risk_decisions": "ts",
    "reconciliation_events": "ts",
    "reconciliation_runs": "started_ts",
    "equity": "ts",
    "order_intents": "created_ts",
    "exchange_order_state": "updated_ts",
}

# never archived/purged (financial records or tiny state tables)
KEEP_ALL_TABLES = {
    "trades", "strategy_runs", "control_commands", "users", "auth_sessions",
    "user_api_keys", "user_strategies", "exchange_position_state",
    "risk_state", "risk_transitions", "notifications", "sqlite_sequence",
}


def log(msg):
    print(f"[db-maintenance] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--archive", required=True)
    ap.add_argument("--retention-days", type=int, default=30)
    ap.add_argument("--vacuum", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cutoff_ms = int((time.time() - args.retention_days * 86400) * 1000)
    log(f"db={args.db} archive={args.archive} retention={args.retention_days}d "
        f"cutoff={cutoff_ms} dry_run={args.dry_run}")

    if not os.path.exists(args.db):
        log(f"FATAL: db not found: {args.db}")
        sys.exit(1)

    size_before = os.path.getsize(args.db)
    log(f"db size before: {size_before / 1e6:.1f} MB")

    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")

    if args.dry_run:
        for table, col in TIME_TABLES.items():
            try:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM main."{table}" WHERE "{col}" < ?',
                    (cutoff_ms,),
                ).fetchone()[0]
                log(f"  [dry-run] {table}: {n} rows older than cutoff")
            except sqlite3.Error as e:
                log(f"  [dry-run] {table}: ERR {e}")
        conn.close()
        return

    os.makedirs(os.path.dirname(args.archive), exist_ok=True)
    # ATTACH the archive to THIS connection so cross-db INSERT works
    conn.execute("ATTACH DATABASE ? AS arch", (args.archive,))
    conn.execute("PRAGMA arch.busy_timeout=60000")

    total_moved = 0
    for table, col in TIME_TABLES.items():
        try:
            old_count = conn.execute(
                f'SELECT COUNT(*) FROM main."{table}" WHERE "{col}" < ?',
                (cutoff_ms,),
            ).fetchone()[0]
            if old_count == 0:
                log(f"  {table}: nothing to archive")
                continue

            # ensure archive table exists with the same columns
            exists = conn.execute(
                "SELECT 1 FROM arch.sqlite_master "
                "WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                conn.execute(
                    f'CREATE TABLE arch."{table}" AS '
                    f'SELECT * FROM main."{table}" WHERE 0')
                log(f"  archive: created table {table}")

            # idempotent: drop previously archived rows for this window first
            conn.execute(
                f'DELETE FROM arch."{table}" WHERE "{col}" < ?', (cutoff_ms,))
            conn.execute(
                f'INSERT INTO arch."{table}" SELECT * FROM main."{table}" '
                f'WHERE "{col}" < ?', (cutoff_ms,))
            conn.execute(
                f'DELETE FROM main."{table}" WHERE "{col}" < ?', (cutoff_ms,))

            # verify before committing: archived count must match source count
            arch_n = conn.execute(
                f'SELECT COUNT(*) FROM arch."{table}" '
                f'WHERE "{col}" < ?', (cutoff_ms,)).fetchone()[0]
            if arch_n != old_count:
                conn.rollback()
                log(f"  {table}: VERIFY FAILED (archived {arch_n} != "
                    f"{old_count}), rolled back")
                continue

            conn.commit()
            total_moved += old_count
            log(f"  {table}: archived + purged {old_count} rows (verified)")
        except sqlite3.Error as e:
            log(f"  {table}: ERROR {e} (rolling back this table)")
            conn.rollback()

    conn.execute("DETACH DATABASE arch")

    log("wal checkpoint (truncate)...")
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        log(f"  checkpoint warning: {e}")

    if args.vacuum:
        log("vacuuming (this can take a while)...")
        conn.execute("VACUUM")
        log("vacuum done")

    conn.execute("PRAGMA optimize")
    conn.close()

    size_after = os.path.getsize(args.db)
    log(f"db size after: {size_after / 1e6:.1f} MB "
        f"(freed {(size_before - size_after) / 1e6:.1f} MB), "
        f"rows moved this run: {total_moved}")
    log("DONE")


if __name__ == "__main__":
    main()
