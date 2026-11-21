from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from math import isfinite

class ProtectionState(str, Enum):
    PLAN_READY = "PLAN_READY"
    ENTRY_SUBMITTED = "ENTRY_SUBMITTED"
    ENTRY_FILLED_UNPROTECTED = "ENTRY_FILLED_UNPROTECTED"
    TP_SUBMITTED = "TP_SUBMITTED"
    SL_SUBMITTED = "SL_SUBMITTED"
    POSITION_PROTECTED = "POSITION_PROTECTED"
    FAIL_CLOSED = "FAIL_CLOSED"
    CLOSED = "CLOSED"

@dataclass(frozen=True)
class EntryPlan:
    trade_id: str
    symbol: str
    direction: str
    entry_price: float
    quantity: float
    stop_loss_price: float
    take_profit_price: float
    max_loss_usdt: float
    max_notional_usdt: float
    strategy_id: str = "one_way_trend"

@dataclass(frozen=True)
class ProtectionSnapshot:
    state: ProtectionState
    position_qty: float = 0.0
    tp_qty: float = 0.0
    sl_qty: float = 0.0
    tp_order_id: str = ""
    sl_order_id: str = ""
    reason: str = ""

def validate_plan(plan: EntryPlan, *, max_loss_usdt: float, max_notional_usdt: float) -> list[str]:
    errors=[]
    if not plan.trade_id or not plan.symbol or plan.strategy_id != "one_way_trend": errors.append("identity_invalid")
    if plan.direction not in {"long", "short"}: errors.append("direction_invalid")
    values=(plan.entry_price,plan.quantity,plan.stop_loss_price,plan.take_profit_price,plan.max_loss_usdt,plan.max_notional_usdt)
    if any(not isfinite(float(x)) or float(x)<=0 for x in values): errors.append("numeric_invalid")
    if plan.direction == "long" and not (plan.stop_loss_price < plan.entry_price < plan.take_profit_price): errors.append("price_direction_invalid")
    if plan.direction == "short" and not (plan.take_profit_price < plan.entry_price < plan.stop_loss_price): errors.append("price_direction_invalid")
    if plan.max_loss_usdt > max_loss_usdt + 1e-9: errors.append("max_loss_exceeded")
    if plan.max_notional_usdt > max_notional_usdt + 1e-9: errors.append("max_notional_exceeded")
    if plan.quantity * plan.entry_price > plan.max_notional_usdt + 1e-6: errors.append("notional_mismatch")
    return errors

def after_entry_fill(plan: EntryPlan, filled_qty: float) -> ProtectionSnapshot:
    if filled_qty <= 0 or filled_qty > plan.quantity + 1e-9:
        return ProtectionSnapshot(ProtectionState.FAIL_CLOSED, reason="fill_quantity_invalid")
    return ProtectionSnapshot(ProtectionState.ENTRY_FILLED_UNPROTECTED, position_qty=filled_qty)

def apply_tp(snapshot: ProtectionSnapshot, order_id: str, covered_qty: float) -> ProtectionSnapshot:
    if snapshot.state not in {ProtectionState.ENTRY_FILLED_UNPROTECTED, ProtectionState.SL_SUBMITTED, ProtectionState.TP_SUBMITTED}:
        return ProtectionSnapshot(ProtectionState.FAIL_CLOSED, snapshot.position_qty, reason="tp_state_invalid")
    if not order_id or covered_qty < snapshot.position_qty - 1e-9:
        return ProtectionSnapshot(ProtectionState.FAIL_CLOSED, snapshot.position_qty, covered_qty, snapshot.sl_qty, reason="tp_coverage_incomplete")
    return ProtectionSnapshot(ProtectionState.TP_SUBMITTED, snapshot.position_qty, covered_qty, snapshot.sl_qty, order_id, snapshot.sl_order_id)

def apply_sl(snapshot: ProtectionSnapshot, order_id: str, covered_qty: float) -> ProtectionSnapshot:
    if snapshot.state not in {ProtectionState.ENTRY_FILLED_UNPROTECTED, ProtectionState.TP_SUBMITTED, ProtectionState.SL_SUBMITTED}:
        return ProtectionSnapshot(ProtectionState.FAIL_CLOSED, snapshot.position_qty, reason="sl_state_invalid")
    if not order_id or covered_qty < snapshot.position_qty - 1e-9:
        return ProtectionSnapshot(ProtectionState.FAIL_CLOSED, snapshot.position_qty, snapshot.tp_qty, covered_qty, snapshot.tp_order_id, reason="sl_coverage_incomplete")
    state=ProtectionState.POSITION_PROTECTED if snapshot.tp_qty >= snapshot.position_qty - 1e-9 else ProtectionState.SL_SUBMITTED
    return ProtectionSnapshot(state, snapshot.position_qty, snapshot.tp_qty, covered_qty, snapshot.tp_order_id, order_id)

def can_open_new(snapshot: ProtectionSnapshot) -> bool:
    return snapshot.state == ProtectionState.POSITION_PROTECTED
