#!/bin/bash
# Auto config backup
mkdir -p data/config_backups
cp config/config.yaml data/config_backups/config.yaml.$(date +%Y%m%d_%H%M%S) 2>/dev/null

cd /opt/okx_quant
exec venv/bin/python3 main.py
