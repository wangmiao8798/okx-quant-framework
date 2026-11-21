"""通知模块 - 邮件发送 + 站内通知存储"""
import logging
import os
import smtplib
import threading
import time
from email.mime.text import MIMEText


class EmailErrorHandler(logging.Handler):
    def __init__(self, notifier):
        super().__init__(logging.ERROR)
        self.notifier = notifier

    def emit(self, record):
        try:
            message = self.format(record)
            self.notifier.alert(
                f"系统异常 {record.levelname}",
                message,
                key=f"{record.name}:{record.getMessage()[:160]}",
            )
        except Exception:
            pass


class Notifier:
    def __init__(self, config_path="/opt/okx_quant/config/config.yaml"):
        self._cfg = {}
        self._last_alerts = {}
        self._lock = threading.Lock()
        try:
            import yaml
            config = config_path if isinstance(config_path, dict) else yaml.safe_load(open(config_path))
            self._cfg = (config or {}).get("notify", {}).get("email", {})
        except Exception:
            pass
        password_env = str(self._cfg.get("password_env") or "").strip()
        if password_env and not self._cfg.get("password"):
            self._cfg["password"] = os.getenv(password_env, "")
        email_env = str(self._cfg.get("email_env") or "").strip()
        if email_env and os.getenv(email_env):
            self._cfg["email"] = os.getenv(email_env, "")
        to_env = str(self._cfg.get("to_env") or "").strip()
        if to_env and os.getenv(to_env):
            self._cfg["to"] = os.getenv(to_env, "")
        self._min_interval = max(1, int(self._cfg.get("min_interval", 300)))

    def send_email(self, subject: str, body: str) -> bool:
        if not self._cfg.get("enabled"): return False
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = self._cfg.get("email", "")
            msg["To"] = self._cfg.get("to", "")
            with smtplib.SMTP_SSL(
                self._cfg.get("smtp_server", "smtp.qq.com"),
                int(self._cfg.get("smtp_port", 465)),
                timeout=8,
            ) as s:
                s.login(self._cfg.get("email",""), self._cfg.get("password",""))
                s.send_message(msg)
            return True
        except Exception as e:
            print(f"[通知] 邮件发送失败: {e}")
            return False

    def send(self, msg: str):
        self.send_email("[量化] 系统通知", msg)

    def alert(self, title: str, body: str, key: str = "") -> bool:
        if not self._cfg.get("enabled"):
            return False
        now = time.time()
        alert_key = key or title
        with self._lock:
            if now - self._last_alerts.get(alert_key, 0.0) < self._min_interval:
                return False
            self._last_alerts[alert_key] = now
        threading.Thread(
            target=self.send_email,
            args=(f"[量化告警] {title}", body),
            daemon=True,
        ).start()
        return True

    def attach_error_logger(self, logger) -> EmailErrorHandler:
        handler = EmailErrorHandler(self)
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
        ))
        logger.addHandler(handler)
        return handler

    def notify(self, user_id: int, title: str, content: str, strategy_id: int = 0):
        """发送通知: 存储到DB + 尝试发邮件"""
        try:
            from utils.database import Database
            db = Database("data/trading.db")
            ts = int(time.time() * 1000)
            db._conn.execute(
                "INSERT INTO notifications(user_id,ts,title,content,strategy_id,is_read) VALUES(?,?,?,?,?,0)",
                (user_id, ts, title, content, strategy_id)
            )
            db._conn.commit()
            db.close()
        except: pass
        self.send_email(f"[量化] {title}", content)
