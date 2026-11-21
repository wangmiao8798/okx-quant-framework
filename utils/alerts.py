
import os, json, requests, time
from datetime import datetime

class AlertManager:
    """Alert system for trading notifications"""
    def __init__(self, config_path="/opt/okx_quant/config/config.yaml"):
        self.webhook_url = ""
        self.min_interval = 300  # 5 min between same alerts
        self._last_alerts = {}
        self._load_config(config_path)
    
    def _load_config(self, path):
        try:
            import yaml
            with open(path) as f: cfg = yaml.safe_load(f)
            alert_cfg = cfg.get("alert", {})
            self.webhook_url = alert_cfg.get("webhook_url", "")
            self.min_interval = alert_cfg.get("min_interval", 300)
        except:
            pass
    
    def send(self, title, msg, level="info"):
        """Send alert via webhook (Telegram/Bark compatible)"""
        now = time.time()
        key = title[:50]
        if key in self._last_alerts and now - self._last_alerts[key] < self.min_interval:
            return  # rate limit
        self._last_alerts[key] = now
        if not self.webhook_url:
            return
        try:
            payload = {"title": title, "content": msg, "level": level, "timestamp": datetime.now().isoformat()}
            requests.post(self.webhook_url, json=payload, timeout=5)
        except:
            pass
    
    def check_and_alert(self, equity, pnl, daily_loss=0):
        alerts = []
        if equity > 0 and equity < 50:
            alerts.append(("CRITICAL: Low Equity", f"Account equity ${equity:.2f} < $50", "critical"))
        if pnl is not None and pnl < -20:
            alerts.append(("Large Loss Alert", f"PnL ${pnl:.2f} exceeded -$20", "warning"))
        if daily_loss > 20:
            alerts.append(("Daily Loss Limit", f"Daily loss ${daily_loss:.2f} > $20", "warning"))
        for t, m, l in alerts:
            self.send(t, m, l)
