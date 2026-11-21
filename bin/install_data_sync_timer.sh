#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/okx_quant
SERVICE=okx-trading-data-sync.service
TIMER=okx-trading-data-sync.timer
PYTHON=/usr/bin/python3
SYNC_SCRIPT="$ROOT/scripts/sync_trading_data.py"
DB_PATH="$ROOT/data/trading.db"
CONTROL="$ROOT/bin/control.sh"

if [[ ! -x "$PYTHON" ]]; then
  echo "missing executable: $PYTHON" >&2
  exit 1
fi
if [[ ! -f "$SYNC_SCRIPT" ]]; then
  echo "missing sync script: $SYNC_SCRIPT" >&2
  exit 1
fi
if [[ ! -x "$CONTROL" ]]; then
  echo "missing executable: $CONTROL" >&2
  exit 1
fi

tmp_dir=$(mktemp -d)
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

service_file="$tmp_dir/$SERVICE"
timer_file="$tmp_dir/$TIMER"

cat >"$service_file" <<'UNIT'
[Unit]
Description=Sync OKX trading runtime data into SQLite
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/okx_quant
ExecStart=/usr/bin/python3 /opt/okx_quant/scripts/sync_trading_data.py
Nice=5
UNIT

cat >"$timer_file" <<'UNIT'
[Unit]
Description=Run OKX trading data sync every 60 seconds

[Timer]
OnBootSec=30sec
OnUnitActiveSec=60sec
AccuracySec=1sec
Persistent=true
Unit=okx-trading-data-sync.service

[Install]
WantedBy=timers.target
UNIT

sudo install -m 0644 -o root -g root "$service_file" "/etc/systemd/system/$SERVICE"
sudo install -m 0644 -o root -g root "$timer_file" "/etc/systemd/system/$TIMER"

sudo systemctl daemon-reload
sudo systemctl enable --now "$TIMER"
if ! sudo systemctl start "$SERVICE"; then
  echo "service start failed; recent journal follows" >&2
  sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
  exit 1
fi

echo "== timer =="
printf 'enabled: '
sudo systemctl is-enabled "$TIMER"
printf 'active: '
sudo systemctl is-active "$TIMER"

echo "== list-timers =="
sudo systemctl list-timers --all "$TIMER" --no-pager

echo "== journal: $SERVICE =="
sudo journalctl -u "$SERVICE" -n 30 --no-pager || true

echo "== sqlite counts: $DB_PATH =="
if [[ -f "$DB_PATH" ]]; then
  "$PYTHON" - "$DB_PATH" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1]).resolve()
uri = db_path.as_uri() + "?mode=ro"
tables = ("trades", "equity", "strategy_status")

with sqlite3.connect(uri, uri=True) as conn:
    for table in tables:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        count = "missing" if exists is None else conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count}")
PY
else
  echo "trades: database missing"
  echo "equity: database missing"
  echo "strategy_status: database missing"
fi

echo "== control health =="
"$CONTROL" health
