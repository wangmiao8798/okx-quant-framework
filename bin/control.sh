#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/okx_quant
SERVICE=okx-quant.service
WEB_SERVICE=okx-quant-web.service
LOG="$ROOT/logs/trading.log"
PY="$ROOT/venv/bin/python3"

usage() {
  echo "Usage: $0 {status|health|logs|check|restart|restart-web|help}"
}

service_status() {
  echo "== services =="
  systemctl is-active "$SERVICE" || true
  systemctl status "$SERVICE" --no-pager | head -35 || true
  echo
  systemctl is-active "$WEB_SERVICE" || true
  systemctl status "$WEB_SERVICE" --no-pager | head -25 || true
}

syntax_check() {
  echo "== syntax check =="
  cd "$ROOT"
  "$PY" -m py_compile strategies/futures_hedge.py core/engine.py risk/*.py
  echo "syntax_ok"
}

health() {
  echo "== health =="
  cd "$ROOT"
  "$PY" - <<'PY'
import json, pathlib, time, datetime
p = pathlib.Path("data/strategies_status.json")
if not p.exists():
    print("status_json_missing")
    raise SystemExit(0)
s = json.loads(p.read_text())
strategies = s.get("strategies") or {}
updated = s.get("updated_at", 0)
print("updated_at", datetime.datetime.fromtimestamp(updated).isoformat() if updated else "")
print("age_sec", round(time.time() - updated, 1) if updated else "")
print("strategy_count", len(strategies))
if not strategies:
    print("strategies_missing")
for name, f in sorted(strategies.items()):
    mon = f.get("monitor") or {}
    print(f"-- {name} {f.get('symbol') or '?'} --")
    for k in ["running", "halted", "cached_equity", "cached_avail", "daily_pnl", "long_qty", "short_qty"]:
        print(k, f.get(k))
    for k in ["margin_load_pct", "account_margin_load_pct", "strategy_cost_pct", "tp_order_count", "grid_long_count", "grid_short_count", "long_add_block_reason", "short_add_block_reason"]:
        print(k, mon.get(k))
PY
  echo "== recent errors =="
  grep -Ei "ERROR|Traceback|Exception|failed|失败" "$LOG" 2>/dev/null | tail -30 || true
}

logs() {
  echo "== logs =="
  tail -120 "$LOG" 2>/dev/null || true
}

restart_trading() {
  syntax_check
  echo "== restart $SERVICE =="
  sudo /bin/systemctl restart "$SERVICE"
  sleep 8
  systemctl is-active "$SERVICE"
  health
}

restart_web() {
  echo "== restart $WEB_SERVICE =="
  sudo /bin/systemctl restart "$WEB_SERVICE"
  sleep 3
  systemctl is-active "$WEB_SERVICE"
  systemctl status "$WEB_SERVICE" --no-pager | head -25 || true
}

case "${1:-help}" in
  status) service_status ;;
  health) health ;;
  logs) logs ;;
  check) syntax_check ;;
  restart) restart_trading ;;
  restart-web) restart_web ;;
  help|-h|--help) usage ;;
  *) usage; exit 2 ;;
esac
