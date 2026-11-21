#!/usr/bin/env python3
"""Rolling retention maintenance for the read-only research/monitor databases.

Those databases grow unbounded because (unlike trading.db) they had no
maintenance task. This script trims rows older than the retention window and
VACUUMs. It is safe to run while the writers are active (WAL + busy_timeout).

Usage:
  python3 research_db_maintenance.py [--retention-days 30] [--dry-run]

Tables handled (db, table, time-column, kind):
  full_market_scanner.db      scanner_signals  captured_at_ms  ms
                              scanner_outcomes observed_at_ms  ms
                              scanner_runs     captured_at_ms  ms
  dram_inventory_shadow.db    snapshots        ts              ms
  cross_market_opportunity.db normalized_quotes batch_id       iso
                              theme_snapshots  batch_id        iso

Rows inside the retention window are never touched. Tables that hold
permanent research output (candidate_intents, mark_outcomes, forward_outcomes,
aligned_window_returns, shadow_signals) are left alone.
"""
import argparse
import datetime
import os
import sqlite3
import sys

DATA = "/opt/okx_quant/data"

PLAN = {
    "full_market_scanner.db": [
        ("scanner_signals", "captured_at_ms", "ms"),
        ("scanner_outcomes", "observed_at_ms", "ms"),
        ("scanner_runs", "captured_at_ms", "ms"),
    ],
    "dram_inventory_shadow.db": [
        ("snapshots", "ts", "ms"),
    ],
    "cross_market_opportunity.db": [
        ("normalized_quotes", "batch_id", "iso"),
        ("theme_snapshots", "batch_id", "iso"),
    ],
}


def log(m):
    print(f"[research-db-maintenance] {m}", flush=True)


def run(retention_days, dry_run):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=retention_days)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    log(f"retention={retention_days}d cutoff={cutoff:%Y-%m-%d %H:%M} "
        f"mode={'DRY-RUN' if dry_run else 'APPLY'}")

    total_deleted = 0
    for db, tables in PLAN.items():
        path = os.path.join(DATA, db)
        if not os.path.exists(path):
            log(f"{db}: not found, skip")
            continue
        size_before = os.path.getsize(path) / 1024 / 1024
        log(f"{db}: {size_before:.1f} MB")

        conn = None
        if not dry_run:
            conn = sqlite3.connect(path, timeout=60)
            conn.execute("PRAGMA busy_timeout=60000")

        for table, tcol, kind in tables:
            cutoff_val = cutoff_ms if kind == "ms" else cutoff_iso
            try:
                if dry_run:
                    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                    before = ro.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    old = ro.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE "{tcol}" < ?',
                        (cutoff_val,),
                    ).fetchone()[0]
                    ro.close()
                    log(f"  {table}: {before} rows, {old} expired (dry-run)")
                else:
                    cur = conn.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE "{tcol}" < ?',
                        (cutoff_val,),
                    )
                    old = cur.fetchone()[0]
                    if old:
                        conn.execute(
                            f'DELETE FROM "{table}" WHERE "{tcol}" < ?', (cutoff_val,)
                        )
                        conn.commit()
                    log(f"  {table}: deleted {old} rows")
                    total_deleted += old
            except Exception as exc:
                log(f"  {table}: FAILED {exc}")

        if conn is not None:
            try:
                conn.execute("VACUUM")
            except Exception as exc:
                log(f"  VACUUM failed: {exc}")
            conn.close()
            size_after = os.path.getsize(path) / 1024 / 1024
            log(f"  -> {size_after:.1f} MB (was {size_before:.1f} MB)")

    if dry_run:
        log("dry-run finished, nothing changed")
    else:
        log(f"done, deleted {total_deleted} rows total")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retention-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return run(args.retention_days, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
