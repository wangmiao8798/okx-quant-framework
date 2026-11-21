"""Single write gateway for all exchange mutations."""

from __future__ import annotations

from risk.policy import normalize_order_intent


class ExecutionGateway:
    def __init__(self, client, risk_gate=None):
        if client is None:
            raise ValueError("execution gateway requires an exchange client")
        self._client = client
        self._risk_gate = risk_gate
        self._risk_context_provider = None

    def set_risk_gate(self, risk_gate):
        self._risk_gate = risk_gate

    def set_risk_context_provider(self, provider):
        self._risk_context_provider = provider

    def _audit_mutation(self, operation: str, symbol: str = "", payload=None):
        risk_gate = self._risk_gate
        if risk_gate is not None:
            decision = risk_gate.evaluate_mutation(operation, symbol=symbol, payload=payload)
            if not decision.get("allowed"):
                return False
        ledger = getattr(self._client, "_event_ledger", None)
        if ledger is not None:
            try:
                ledger.append_event(
                    None,
                    "MUTATION_REQUESTED",
                    operation=operation,
                    payload={"symbol": symbol, **(payload or {})},
                )
            except Exception:
                pass
        return True

    def place_order(self, symbol: str, side: str, quantity, **kwargs):
        risk_gate = self._risk_gate
        supplied_context = kwargs.pop("risk_context", None)
        paired_net_neutral = bool(kwargs.pop("paired_net_neutral", False))
        inventory_repair = bool(kwargs.pop("inventory_repair", False))
        supplied_intent = kwargs.pop("intent_type", None)
        payload = {
            "quantity": str(quantity),
            **kwargs,
            "paired_net_neutral": paired_net_neutral,
            "inventory_repair": inventory_repair,
        }
        if supplied_intent:
            payload["intent_type"] = supplied_intent
        intent, intent_error = normalize_order_intent(payload, bool(kwargs.get("reduce_only", False)))
        payload["intent_type"] = intent
        if intent_error:
            return {
                "sCode": "risk_gate",
                "sMsg": "order_intent_invalid",
                "risk_state": "",
                "guard_blocked": True,
            }
        if risk_gate is not None:
            try:
                context = supplied_context
                if context is None and callable(self._risk_context_provider):
                    context = self._risk_context_provider()
            except Exception:
                context = None
            decision = risk_gate.evaluate_order(
                symbol=symbol,
                side=side,
                pos_side=str(kwargs.get("pos_side") or ""),
                reduce_only=bool(kwargs.get("reduce_only", False)),
                payload=payload,
                context=context,
            )
            if not decision.get("allowed"):
                return {
                    "sCode": "risk_gate",
                    "sMsg": decision.get("reason_code", "risk_gate_rejected"),
                    "risk_state": decision.get("state", ""),
                    "guard_blocked": True,
                }
        return self._client.place_order(symbol, side, str(quantity), **kwargs)

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        if not symbol or not order_id:
            return False
        if not self._audit_mutation("cancel_order", symbol, {"order_id": order_id}):
            return False
        response = self._client._call(
            self._client._trade.cancel_order,
            instId=symbol,
            ordId=order_id,
        )
        ok = not isinstance(response, dict) or str(response.get("code", "0")) == "0"
        ledger = getattr(self._client, "_event_ledger", None)
        if ledger is not None:
            try:
                ledger.append_event(
                    None,
                    "MUTATION_RESULT",
                    operation="cancel_order",
                    status_code=str(response.get("code", "0")) if isinstance(response, dict) else "0",
                    message=str(response.get("msg", "")) if isinstance(response, dict) else "",
                    payload={"symbol": symbol, "order_id": order_id, "ok": ok},
                )
            except Exception:
                pass
        return ok

    def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "cross"):
        if not self._audit_mutation("set_leverage", symbol, {"leverage": leverage, "margin_mode": margin_mode}):
            return {"code": "risk_gate", "msg": "risk_gate_rejected"}
        return self._client.set_leverage(symbol, leverage, margin_mode)

    def close_all(self, symbol: str = "", margin_mode: str = "cross"):
        if not self._audit_mutation("close_all", symbol, {"margin_mode": margin_mode}):
            return {"code": "risk_gate", "msg": "risk_gate_rejected"}
        return self._client.close_all(symbol, margin_mode)
