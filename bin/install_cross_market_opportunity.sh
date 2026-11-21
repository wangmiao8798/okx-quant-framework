#!/usr/bin/env bash
set -euo pipefail
root=/opt/okx_quant
service=okx-cross-market-opportunity.service
timer=okx-cross-market-opportunity.timer
"$root/venv/bin/python3" -m py_compile "$root/scripts/cross_market_opportunity_radar.py"
"$root/venv/bin/python3" -m pytest -q "$root/tests/test_cross_market_opportunity_radar.py"
sudo install -m 0644 "$root/deploy/systemd/$service" "/etc/systemd/system/$service"
sudo install -m 0644 "$root/deploy/systemd/$timer" "/etc/systemd/system/$timer"
sudo systemctl daemon-reload
sudo systemctl enable --now "$timer"
systemctl is-active "$timer"
systemctl status "$service" "$timer" --no-pager
