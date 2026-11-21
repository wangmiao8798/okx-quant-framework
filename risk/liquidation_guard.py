import time

from utils.helpers import safe_float
from utils.logger import get_logger


class LiquidationGuard:
    """Protect positions using validated mark-to-liquidation distance."""

    def __init__(self, config, on_lev=None, on_pos=None, on_emer=None):
        self.log = get_logger("risk.liquidation")
        lc = config.get("risk", {}).get("liquidation", {})
        self._enabled = bool(lc.get("enabled", True))
        self._safety_distance = max(0.01, safe_float(lc.get("safety_distance_rate", 0.15)))
        self._emergency_distance = max(
            0.005, min(self._safety_distance, safe_float(lc.get("emergency_distance_rate", 0.07)))
        )
        self._confirmations = max(1, int(safe_float(lc.get("confirmation_checks", 2))))
        self._pause_seconds = max(30.0, safe_float(lc.get("pause_opening_seconds", 120)))
        self._action_cooldown = max(30.0, safe_float(lc.get("action_cooldown_seconds", 60)))
        self._reduce_ratio = min(0.5, max(0.05, safe_float(lc.get("emergency_reduce_ratio", 0.25))))
        self._on_lev = on_lev
        self._on_pos = on_pos
        self._on_emer = on_emer
        self._acts = {"pause": 0, "reduce": 0}
        self._breach_count = 0
        self._last_action = 0.0
        self._last_status = self._status("unavailable")

    def _status(self, zone, **values):
        # Invalid or missing liqPx is observation-only. It must never become
        # an opening block because hedge-mode liqPx is not a reliable account
        # risk signal for this strategy.
        unavailable = zone == "unavailable" and bool(values.get("positions_present", False))
        return {
            "available": False,
            "positions_present": False,
            "zone": zone,
            "liq_px_unavailable": unavailable,
            "liquidation_distance_safe": zone in {"safe", "no_positions", "disabled"},
            "liquidation_warning": zone == "warning",
            "liquidation_emergency": zone == "emergency",
            "pause_opening": False,
            "emergency_reduce": False,
            "min_distance_rate": None,
            "affected_sides": [],
            "reason": "liquidation_distance_unavailable",
            **values,
        }

    @staticmethod
    def _valid_distances(risk):
        mark = safe_float(risk.get("mark_px", 0))
        if mark <= 0:
            return []
        distances = []
        long_sz = abs(safe_float(risk.get("long_sz", 0)))
        long_liq = safe_float(risk.get("long_liq", 0))
        if long_sz > 0 and 0 < long_liq < mark:
            distances.append(("long", (mark - long_liq) / mark))
        short_sz = abs(safe_float(risk.get("short_sz", 0)))
        short_liq = safe_float(risk.get("short_liq", 0))
        if short_sz > 0 and short_liq > mark:
            distances.append(("short", (short_liq - mark) / mark))
        return distances

    def evaluate(self, risk):
        if not self._enabled:
            self._breach_count = 0
            self._last_status = self._status("disabled", reason="liquidation_guard_disabled")
            return dict(self._last_status)

        risk = risk or {}
        mark = safe_float(risk.get("mark_px", 0))
        long_sz = abs(safe_float(risk.get("long_sz", 0)))
        short_sz = abs(safe_float(risk.get("short_sz", 0)))
        invalid_sides = []
        if long_sz > 0 and not (mark > 0 and 0 < safe_float(risk.get("long_liq", 0)) < mark):
            invalid_sides.append("long")
        if short_sz > 0 and not (mark > 0 and safe_float(risk.get("short_liq", 0)) > mark):
            invalid_sides.append("short")
        if invalid_sides:
            self._breach_count = 0
            self._last_status = self._status(
                "unavailable",
                positions_present=True,
                invalid_sides=invalid_sides,
                reason="liquidation_distance_unavailable",
            )
            return dict(self._last_status)

        distances = self._valid_distances(risk)
        if not distances:
            self._breach_count = 0
            positions_present = (
                abs(safe_float((risk or {}).get("long_sz", 0))) > 0
                or abs(safe_float((risk or {}).get("short_sz", 0))) > 0
            )
            self._last_status = self._status(
                "unavailable" if positions_present else "no_positions",
                positions_present=positions_present,
                invalid_sides=[],
                reason="liquidation_distance_unavailable" if positions_present else "no_positions",
            )
            return dict(self._last_status)

        min_distance = min(distance for _, distance in distances)
        affected = [side for side, distance in distances if distance <= self._safety_distance]
        if min_distance > self._safety_distance:
            self._breach_count = 0
            self._last_status = self._status(
                "safe",
                available=True,
                positions_present=True,
                min_distance_rate=min_distance,
                affected_sides=[],
                reason="liquidation_distance_safe",
            )
            return dict(self._last_status)

        self._breach_count += 1
        confirmed = self._breach_count >= self._confirmations
        emergency = min_distance <= self._emergency_distance
        zone = "emergency" if emergency else "warning"
        now = time.time()
        reduce_now = bool(emergency and confirmed and now - self._last_action >= self._action_cooldown)
        if reduce_now:
            self._last_action = now
            self._acts["reduce"] += 1
        elif confirmed and self._breach_count == self._confirmations:
            self._acts["pause"] += 1
        self._last_status = self._status(
            zone,
            available=True,
            positions_present=True,
            pause_opening=True,
            emergency_reduce=reduce_now,
            min_distance_rate=min_distance,
            affected_sides=affected,
            reason=f"liquidation_{zone}",
            confirmed=confirmed,
            confirmation_count=self._breach_count,
        )
        return dict(self._last_status)

    def check_and_act(self, risk, cur_lev=None):
        status = self.evaluate(risk)
        if status.get("emergency_reduce"):
            if self._on_pos:
                self._on_pos(self._reduce_ratio)
            if self._on_emer:
                self._on_emer()
            return "emergency_reduce"
        if status.get("pause_opening"):
            return "pause_opening"
        return None

    @property
    def pause_seconds(self):
        return self._pause_seconds

    @property
    def reduce_ratio(self):
        return self._reduce_ratio

    @property
    def last_status(self):
        return dict(self._last_status)

    def stats(self):
        return {**self._acts, **self.last_status}
