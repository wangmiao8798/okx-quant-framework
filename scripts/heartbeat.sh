#!/bin/bash
PID_FILE=/opt/okx_quant/data/run.pid
LOG_FILE=/opt/okx_quant/data/heartbeat.log
if [ -f "$PID_FILE" ]; then
    PID=$(cat $PID_FILE 2>/dev/null)
    if [ -z "$PID" ] || ! kill -0 $PID 2>/dev/null; then
        echo "[$(date)] Engine PID=$PID dead, restarting..." >> $LOG_FILE
        cd /opt/okx_quant && nohup venv/bin/python3 main.py > /dev/null 2>&1 &
        echo $! > $PID_FILE
    fi
else
    cd /opt/okx_quant && nohup venv/bin/python3 main.py > /dev/null 2>&1 &
    echo $! > $PID_FILE
fi