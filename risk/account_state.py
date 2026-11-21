"""Exchange account balance and margin-risk snapshot state."""

import time

from utils.helpers import safe_float


class AccountRiskState:
    """Own account balance polling and preserve raw OKX risk fields."""

    def __init__(self, client=None, log=None, refresh_seconds: float = 5.0):
        self.client = client
        self.log = log
        self.refresh_seconds = max(0.0, float(refresh_seconds))
        self.cached_avail = 0.0
        self.cached_frozen = 0.0
        self.cached_equity = 0.0
        self.last_check = 0.0
        self.snapshot = {}

    @staticmethod
    def build_snapshot(account: dict, detail: dict, observed_at_ms: int | None = None) -> dict:
        account = account if isinstance(account, dict) else {}
        detail = detail if isinstance(detail, dict) else {}
        account_fields = (
            "uTime", "totalEq", "adjEq", "availEq", "ordFroz",
            "imr", "mmr", "mgnRatio", "notionalUsd",
        )
        detail_fields = (
            "ccy", "eq", "availBal", "availEq", "frozenBal",
            "mmr", "mgnRatio", "upl",
        )
        return {
            "source": "okx_account_balance",
            "observed_at_ms": int(observed_at_ms or time.time() * 1000),
            "account": {key: account.get(key) for key in account_fields},
            "usdt": {key: detail.get(key) for key in detail_fields},
        }

    def sync(self, force: bool = False) -> bool:
        now = time.time()
        if not force and now - self.last_check < self.refresh_seconds:
            return True
        self.last_check = now
        if not self.client:
            return True
        try:
            resp = self.client.get_account_balance_raw()
            for item in (resp or {}).get("data", []):
                usdt_detail = {}
                for detail in item.get("details", []):
                    if detail.get("ccy") == "USDT":
                        usdt_detail = detail
                        self.cached_avail = safe_float(detail.get("availBal", 0))
                        self.cached_frozen = safe_float(detail.get("frozenBal", 0))
                        self.cached_equity = safe_float(detail.get("eq", 0))
                        break
                if not usdt_detail:
                    # A successful HTTP response without a USDT detail is not a
                    # usable margin snapshot. Clear cached values so a stale
                    # balance can never authorize a new order.
                    self.cached_avail = 0.0
                    self.cached_frozen = 0.0
                    self.cached_equity = 0.0
                self.snapshot = self.build_snapshot(item, usdt_detail)
                return bool(usdt_detail)
        except Exception as exc:
            if self.log:
                self.log.warning(f"[account risk] balance sync failed: {exc}")
            return False
        return False

    def log_balance(self, total_pnl: float = 0.0, total_fees: float = 0.0) -> bool:
        if not self.sync(force=True):
            return False
        if self.log and self.client:
            self.log.info(
                f"[balance] eq={self.cached_equity:.2f} avail={self.cached_avail:.2f} "
                f"frozen={self.cached_frozen:.2f} pnl={total_pnl:.2f} fees={total_fees:.4f}"
            )
        return True
