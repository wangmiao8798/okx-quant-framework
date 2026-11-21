"""Persistent, exchange-independent risk gate for execution intents."""

from __future__ import annotations

import time

from risk.policy import RiskPolicy


class RiskGate:
    STATES = {"ACTIVE", "INVENTORY_REPAIR", "REDUCE_ONLY", "HALTED"}

    def __init__(self, event_ledger, reconciler=None, log=None, max_reconciler_age: float = 90.0):
        self.event_ledger = event_ledger
        self.reconciler = reconciler
        self.log = log
        self.max_reconciler_age = max(30.0, float(max_reconciler_age))
        self._state = self.event_ledger.get_or_create_risk_state()

    def refresh(self):
        self._state = self.event_ledger.get_risk_state()
        return self._state

    def set_state(self, state: str, reason: str, actor: str = "system"):
        state = str(state or "").upper()
        if state not in self.STATES:
            raise ValueError(f"invalid risk state: {state}")
        result = self.event_ledger.transition_risk_state(state, reason, actor)
        self.refresh()
        if result.get("changed") and self.log:
            self.log.warning(
                f"[RiskGate] state {self._state['state']} version={self._state['version']} "
                f"reason={reason} actor={actor}"
            )
        return result

    def _record(self, decision):
        try:
            self.event_ledger.record_risk_decision(decision)
        except Exception as exc:
            if self.log:
                self.log.warning(f"[RiskGate] decision record failed: {exc}")
        return decision

    def evaluate_order(self, symbol: str, side: str, pos_side: str, reduce_only: bool, payload=None, context=None):
        state = self.refresh()["state"]
        positions = self.event_ledger.get_exchange_positions(symbol) if state == "INVENTORY_REPAIR" else None
        status = self.reconciler.get_status() if self.reconciler else {}
        decision = RiskPolicy.evaluate_order(
            state,
            symbol,
            side,
            pos_side,
            reduce_only,
            payload=payload,
            reconciler_status=status,
            positions=positions,
            max_reconciler_age=self.max_reconciler_age,
            context=context,
        )
        return self._record(decision)

    def evaluate_mutation(self, operation: str, symbol: str = "", payload=None):
        """Audit non-order exchange writes; cancellations and exits stay allowed."""
        state = self.refresh()["state"]
        decision = RiskPolicy.evaluate_mutation(operation, symbol, payload, state)
        return self._record(decision)

    def get_status(self):
        state = self.refresh()
        reconciler_status = self.reconciler.get_status() if self.reconciler else {}
        last_success = float(reconciler_status.get("last_success") or 0.0)
        return {
            **state,
            "reconciler_ready": bool(reconciler_status.get("ready")),
            "reconciler_age_sec": max(0.0, time.time() - last_success) if last_success else None,
            "max_reconciler_age_sec": self.max_reconciler_age,
        }
