"""全局止损：总亏损达到阈值时停止交易"""
import time

from utils.logger import get_logger
from utils.helpers import safe_float


class TotalLossGuard:
    def __init__(self, config, on_stop=None):
        self.log = get_logger("risk.totalloss")

        risk_stop = config.get("risk", {}).get("stop_loss")
        legacy_stop = config.get("stop_loss")

        if isinstance(risk_stop, dict) and risk_stop:
            rc = risk_stop
            if isinstance(legacy_stop, dict) and legacy_stop:
                conflicts = []
                for key in ("enabled", "total_loss_pct", "cooldown_seconds"):
                    if key in legacy_stop and legacy_stop.get(key) != risk_stop.get(key):
                        conflicts.append(key)
                if conflicts:
                    self.log.warning(
                        "检测到 stop_loss 与 risk.stop_loss 配置冲突，"
                        f"优先使用 risk.stop_loss: {', '.join(conflicts)}"
                    )
        elif isinstance(legacy_stop, dict) and legacy_stop:
            rc = legacy_stop
            self.log.warning("使用兼容配置 stop_loss，建议迁移到 risk.stop_loss")
        else:
            rc = {}

        self._enabled = rc.get("enabled", True)
        self._pct = rc.get("total_loss_pct", 12)
        self._cool = rc.get("cooldown_seconds", 600)
        self._on_stop = on_stop
        self._frozen = False
        self._since = 0.0
        self._initial_eq = 0.0
        self._last_valid_eq = 0.0
        self._data_unavailable_since = 0.0
        self._data_grace_seconds = max(
            5.0, safe_float(rc.get("account_data_grace_seconds", 30))
        )
        self._halt_reason = ""

    def set_initial_equity(self, eq: float):
        eq = safe_float(eq)
        if eq > 0:
            self._initial_eq = eq
            self._last_valid_eq = eq

    def check(self, current_eq: float) -> bool:
        """检查总亏损，返回True=安全，False=触发止损"""
        if not self._enabled:
            return True
        current = safe_float(current_eq)
        now = time.time()
        if current <= 0:
            if self._data_unavailable_since <= 0:
                self._data_unavailable_since = now
            if now - self._data_unavailable_since < self._data_grace_seconds:
                return True
            if not self._frozen or self._halt_reason != "account_data_unavailable":
                self.log.error(
                    "账户权益数据持续不可用，暂停新开仓并保留减仓能力"
                )
            self._frozen = True
            self._halt_reason = "account_data_unavailable"
            self._since = self._data_unavailable_since
            return False

        self._last_valid_eq = current
        self._data_unavailable_since = 0.0
        if self._frozen and self._halt_reason == "account_data_unavailable":
            self._frozen = False
            self._halt_reason = ""
            self.log.info("账户权益数据恢复，解除数据不可用保护")

        if self._frozen:
            if now - self._since >= self._cool:
                self._frozen = False
                self._halt_reason = ""
                self._initial_eq = current_eq
                self.log.info("止损冷却结束，重置权益追踪")
            return True

        if self._initial_eq <= 0:
            self._initial_eq = current
            return True

        loss_pct = max(0, (self._initial_eq - current) / self._initial_eq * 100)
        if loss_pct >= self._pct:
            self._frozen = True
            self._since = now
            self._halt_reason = "global_total_loss"
            self.log.warning(f"全局止损触发: 亏损{loss_pct:.1f}% >= {self._pct}%")
            if self._on_stop:
                self._on_stop()
            return False
        return True

    @property
    def halted(self):
        return self._frozen

    @property
    def remaining(self):
        if not self._frozen:
            return 0
        return max(0, self._cool - (time.time() - self._since))

    @property
    def halt_reason(self):
        return self._halt_reason

    @property
    def data_unavailable(self):
        return self._halt_reason == "account_data_unavailable"

    @property
    def last_valid_equity(self):
        return self._last_valid_eq
