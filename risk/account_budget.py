"""Shared account-level incremental margin budget for multi-symbol strategies."""

from __future__ import annotations

import threading
import time
import uuid

from utils.helpers import safe_float


def order_margin_requirement(
    notional_usdt: float,
    leverage: float,
    buffer_ratio: float = 0.10,
) -> float:
    """Estimate incremental initial margin plus a small execution buffer."""
    notional = max(0.0, safe_float(notional_usdt))
    lev = max(1.0, safe_float(leverage, 1.0))
    buffer = max(0.0, min(0.50, safe_float(buffer_ratio, 0.10)))
    return notional / lev * (1.0 + buffer)


def account_incremental_margin_budget(
    equity: float,
    available_margin: float,
    target_margin_ratio: float = 0.45,
    reserve_equity_ratio: float = 0.10,
    utilization: float = 0.80,
) -> float:
    """Return the account-wide margin budget available for new orders.

    Existing frozen margin is already reflected in exchange available margin.
    The reserve and utilization haircut prevent three independent strategy
    instances from each spending the same apparent free balance.
    """
    equity = max(0.0, safe_float(equity))
    available = max(0.0, safe_float(available_margin))
    reserve_ratio = max(0.0, min(0.50, safe_float(reserve_equity_ratio, 0.10)))
    target_ratio = max(0.0, min(1.0, safe_float(target_margin_ratio, 0.45)))
    utilization = max(0.0, min(1.0, safe_float(utilization, 0.80)))
    reserve = equity * reserve_ratio
    return max(0.0, min(available - reserve, equity * target_ratio) * utilization)


class AccountBudgetAllocator:
    """Thread-safe reservations shared by all strategy profiles in one engine."""

    def __init__(
        self,
        target_margin_ratio: float = 0.45,
        reserve_equity_ratio: float = 0.10,
        utilization: float = 0.80,
        reservation_ttl_sec: float = 20.0,
    ):
        self.target_margin_ratio = max(0.0, min(1.0, safe_float(target_margin_ratio, 0.45)))
        self.reserve_equity_ratio = max(0.0, min(0.50, safe_float(reserve_equity_ratio, 0.10)))
        self.utilization = max(0.0, min(1.0, safe_float(utilization, 0.80)))
        self.reservation_ttl_sec = max(3.0, safe_float(reservation_ttl_sec, 20.0))
        self._lock = threading.RLock()
        self._equity = 0.0
        self._available_margin = 0.0
        self._frozen_margin = 0.0
        self._mgn_ratio = 0.0
        self._updated_at = 0.0
        self._reservations = {}

    def update(self, equity: float, available_margin: float, frozen_margin: float = 0.0, mgn_ratio=0.0):
        with self._lock:
            self._equity = max(0.0, safe_float(equity))
            self._available_margin = max(0.0, safe_float(available_margin))
            self._frozen_margin = max(0.0, safe_float(frozen_margin))
            self._mgn_ratio = max(0.0, safe_float(mgn_ratio))
            self._updated_at = time.time()
            self._prune_locked()

    def _prune_locked(self):
        now = time.time()
        self._reservations = {
            token: item
            for token, item in self._reservations.items()
            if item["expires_at"] > now
        }

    def total_budget(self) -> float:
        with self._lock:
            self._prune_locked()
            return account_incremental_margin_budget(
                self._equity,
                self._available_margin,
                self.target_margin_ratio,
                self.reserve_equity_ratio,
                self.utilization,
            )

    def reserved_margin(self) -> float:
        with self._lock:
            self._prune_locked()
            return sum(item["margin"] for item in self._reservations.values())

    def remaining_margin(self) -> float:
        with self._lock:
            self._prune_locked()
            return max(0.0, self.total_budget() - self.reserved_margin())

    def reserve(self, margin_required: float, symbol: str = "", purpose: str = "open") -> str:
        margin_required = max(0.0, safe_float(margin_required))
        with self._lock:
            self._prune_locked()
            if margin_required <= 0.0:
                return ""
            if margin_required > self.total_budget() - self.reserved_margin() + 1e-9:
                return ""
            token = uuid.uuid4().hex
            self._reservations[token] = {
                "margin": margin_required,
                "symbol": str(symbol or ""),
                "purpose": str(purpose or "open"),
                "expires_at": time.time() + self.reservation_ttl_sec,
            }
            return token

    def release(self, token: str):
        if not token:
            return
        with self._lock:
            self._reservations.pop(str(token), None)

    def commit(self, token: str):
        """Keep a successful order reservation briefly until balance refresh."""
        if not token:
            return
        with self._lock:
            item = self._reservations.get(str(token))
            if item:
                item["expires_at"] = time.time() + self.reservation_ttl_sec

    def snapshot(self) -> dict:
        with self._lock:
            self._prune_locked()
            return {
                "enabled": self._equity > 0.0,
                "equity": self._equity,
                "available_margin": self._available_margin,
                "frozen_margin": self._frozen_margin,
                "mgn_ratio": self._mgn_ratio,
                "total_budget_margin": self.total_budget(),
                "reserved_margin": self.reserved_margin(),
                "remaining_margin": self.remaining_margin(),
                "reservation_count": len(self._reservations),
                "updated_at": self._updated_at,
            }
