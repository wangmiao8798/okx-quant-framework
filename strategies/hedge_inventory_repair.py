# -*- coding: utf-8 -*-
"""库存修复/超额回撤编排函数，从 FuturesHedgeStrategy 提取（拆分第四阶段）。

设计约束（拆分契约）：
- 编排器方法保持策略实例签名（self 作为显式受控适配器），函数体与旧实现
  逐字一致，保证行为零差异；所有副作用（下单、撤单、写状态、记录事件、
  日志）仍经 self 上的受控接口执行，由原方法代理转发。
- 编排器只被 _check_add 等策略编排器调用；本模块不 import
  strategies.futures_hedge，避免循环依赖（self 在调用时注入）。
"""
from __future__ import annotations

import math
import time

from utils.helpers import safe_float


def maybe_repair_normal_close_missing_side(self, price: float):
    """Restore a normally closed weak side only after all safety gates pass."""
    self._missing_side_repair_last_reason = ""
    if not bool(getattr(self, "_conditional_missing_side_repair_enabled", False)):
        return None
    if not self._client or price <= 0 or self._trading_halted:
        return None
    if getattr(self, "_reduce_only_mode", False) or self._risk_opening_pause_active():
        self._missing_side_repair_last_reason = "opening_paused"
        return None
    if self._in_startup_recovery() or self._in_post_recovery_refill_delay():
        self._missing_side_repair_last_reason = "recovery_mode"
        return None

    now = time.time()
    cooldown = safe_float(getattr(self, "_missing_side_repair_cooldown_sec", 180.0))
    if now - safe_float(getattr(self, "_last_missing_side_repair_ts", 0.0)) < cooldown:
        self._missing_side_repair_last_reason = "cooldown"
        return None

    long_qty = safe_float(getattr(self._long, "qty", 0.0))
    short_qty = safe_float(getattr(self._short, "qty", 0.0))
    tolerance = self._position_qty_tolerance()
    long_missing = long_qty <= tolerance and short_qty > tolerance
    short_missing = short_qty <= tolerance and long_qty > tolerance
    if not long_missing and not short_missing:
        return None

    missing_side = "long" if long_missing else "short"
    dominant_side = "short" if long_missing else "long"
    normal_close = bool(getattr(self, "_normal_close_sides", {}).get(missing_side, False))
    allow_legacy = bool(getattr(self, "_missing_side_repair_allow_legacy_state", False))
    if not normal_close and not allow_legacy:
        self._missing_side_repair_last_reason = "missing_side_not_normal_close"
        return None

    imbalance = self._net_imbalance_ratio()
    threshold = safe_float(getattr(self, "_missing_side_repair_imbalance_threshold", 0.70))
    if imbalance < threshold:
        self._missing_side_repair_last_reason = f"imbalance_below_threshold_{imbalance:.3f}"
        return None

    equity = safe_float(getattr(self, "_cached_equity", 0.0))
    if equity <= 0:
        self._missing_side_repair_last_reason = "account_state_unavailable"
        return None
    margin_load = self._account_margin_load_ratio()
    # This path reduces net exposure, so use its explicit account-level cap
    # instead of the stricter per-side opening cap.
    margin_cap = safe_float(getattr(self, "_missing_side_repair_max_margin_ratio", 0.30))
    if margin_load >= margin_cap:
        self._missing_side_repair_last_reason = f"margin_load_{margin_load:.3f}"
        return None

    # Do not add a second order while the first recovery order is still live.
    pending_qty = self._pending_open_qty(missing_side)
    if pending_qty is None:
        self._missing_side_repair_last_reason = "live_order_state_unavailable"
        return None
    if pending_qty > tolerance:
        self._missing_side_repair_last_reason = "pending_open_order"
        return None

    # The existing position must be fully protected before we add the weak side.
    dominant_pos = self._long if dominant_side == "long" else self._short
    if not bool(getattr(self, "_tp_init_done", False)):
        self._missing_side_repair_last_reason = "tp_not_initialized"
        return None
    if self._risk_tp_covered_qty(dominant_side) + tolerance < dominant_pos.qty:
        self._missing_side_repair_last_reason = "dominant_tp_coverage_incomplete"
        self._set_add_block(missing_side, self._missing_side_repair_last_reason)
        return None

    result = self._do_add(missing_side, price, reason_code="missing_side_repair")
    if isinstance(result, dict) and result.get("type") == "add":
        self._last_missing_side_repair_ts = now
        self._missing_side_repair_last_reason = "submitted"
        self._record_event(
            "missing_side_repair",
            missing_side,
            "normal_close_imbalance",
            detail=f"dominant={dominant_side} imbalance={imbalance:.3f}",
            price=price,
            qty=safe_float(result.get("quantity")),
        )
        return result
    self._missing_side_repair_last_reason = str(
        (result or {}).get("reason", "opening_guard") if isinstance(result, dict) else "opening_guard"
    )
    return None


def maybe_active_inventory_repair(self, price: float):
    if not bool(getattr(self, "_active_inventory_repair_enabled", False)) or price <= 0:
        return None
    plan = self._inventory_repair_plan(price, update_mode=True)
    if not plan.get("active"):
        return None
    repair_side = str(plan.get("repair_side") or "")
    layer = self._long_layer if repair_side == "long" else self._short_layer
    if layer >= self._effective_side_max_adds(repair_side) and not bool(getattr(self, "_inventory_repair_ignore_layer_cap", False)):
        self._set_add_block(repair_side, "inventory_repair_max_layers")
        return {"type": "skip", "reason": "inventory_repair_max_layers"}
    try:
        pending_qty = self._pending_repair_qty(repair_side, price)
    except Exception:
        self._set_add_block(repair_side, "inventory_repair_order_state_unavailable")
        return {"type": "skip", "reason": "inventory_repair_order_state_unavailable"}
    uncovered_qty = max(0.0, safe_float(plan.get("proposed_qty")) - pending_qty)
    if uncovered_qty + 1e-12 < self._min_qty:
        self._set_add_block(repair_side, "inventory_repair_pending")
        return {"type": "skip", "reason": "inventory_repair_pending"}
    now = time.time()
    repair_cooldown = self._inventory_repair_cooldown(safe_float(plan.get("net_notional")))
    if now - safe_float(getattr(self, "_last_inventory_repair_ts", 0.0)) < repair_cooldown:
        self._set_add_block(repair_side, "inventory_repair_cooldown")
        return {"type": "skip", "reason": "inventory_repair_cooldown"}
    from strategies.hedge_calculations import contract_qty_for_margin
    base_qty = contract_qty_for_margin(
        self._effective_base_pos_usdt(repair_side),
        price,
        self._cur_lev,
        self._ct_val,
        self._qty_step,
    )
    order_qty_cap = min(uncovered_qty, max(self._min_qty, base_qty))
    order_qty_cap = self._inventory_repair_step_qty_cap(
        repair_side, price, order_qty_cap
    )
    # The ordinary inventory pause is keyed to the grid bias threshold.
    # The CRV corridor is checked against its exact proposed quantity so
    # a future base-size change cannot silently cross the 63% ceiling.
    if self._inventory_repair_margin_blocked(
        repair_side, price, order_qty_cap
    ):
        self._set_add_block(repair_side, "inventory_repair_margin")
        return {"type": "skip", "reason": "inventory_repair_margin"}
    if order_qty_cap + 1e-12 < self._min_qty:
        self._set_add_block(repair_side, "inventory_repair_step_cap")
        return {"type": "skip", "reason": "inventory_repair_step_cap"}
    # A repair order replaces the same-side passive grid reservation.
    # Remove stale opening grids first so their pending quantity cannot
    # consume the weak-side repair capacity a second time. Reduce-only TP
    # orders are intentionally preserved.
    if (
        bool(getattr(self, "_active_inventory_repair_enabled", False))
        and not self._preserve_opening_grids_during_inventory_repair()
    ):
        cancelled = self._cancel_opening_orders()
        if cancelled:
            self.log.info(
                f"[inventory_repair] {self._symbol} cancelled {cancelled} stale opening grids"
            )
    # Size the repair against the same explicit side/total/net/budget
    # limits used at submission. This prevents a large planned repair from
    # repeatedly hitting the ordinary max_side_notional guard.
    repair_capacity = self._grid_capacity_qty(
        repair_side,
        order_qty_cap,
        price,
        pending_orders={"long": 0.0, "short": 0.0},
        inventory_repair=True,
    )
    if repair_capacity + 1e-12 < self._min_qty:
        self._set_add_block(repair_side, "inventory_repair_capacity_exhausted")
        return {"type": "skip", "reason": "inventory_repair_capacity_exhausted"}
    order_qty_cap = min(order_qty_cap, repair_capacity)
    result = self._do_add(
        repair_side,
        price,
        max_qty=order_qty_cap,
        reason_code="inventory_repair",
        allow_hard_total_cap=True,
    )
    if result.get("type") == "add":
        self._last_inventory_repair_ts = now
        if bool(getattr(self, "_coverage_safety_tp_repair_incident", {}).get(repair_side)):
            self._consume_coverage_safety_tp_repair(repair_side)
        self._record_event(
            "inventory_repair",
            repair_side,
            "active_repair",
            detail=f"planned={safe_float(plan.get('proposed_qty')):.4f} pending={pending_qty:.4f}",
            price=price,
            qty=safe_float(result.get("quantity")),
        )
    else:
        self.log.warning(
            f"[inventory_repair] {repair_side} skipped reason={result.get('reason', 'unknown')} "
            f"planned={safe_float(plan.get('proposed_qty')):.4f} pending={pending_qty:.4f} cap={order_qty_cap:.4f}"
        )
    return result


def maybe_over_cap_unwind(self, price: float):
    """Reduce an established CRV inventory that is already over capacity.

    Opening guards correctly reject further gross exposure, but they cannot
    resolve a position created under an older limit.  This path is therefore
    intentionally separate from ``_do_add`` and only ever submits a bounded
    reduce-only market order.  The exchange position is refreshed before the
    next attempt so a stale local quantity cannot cause a 51169 retry loop.
    """
    if not bool(getattr(self, "_over_cap_unwind_enabled", False)):
        return None
    symbol = str(getattr(self, "_symbol", "")).upper()
    if not symbol.startswith("CRV-") or not getattr(self, "_client", None) or price <= 0:
        return None
    margin_load = safe_float(self._account_margin_load_ratio())
    hard_margin = safe_float(getattr(self, "_account_margin_hard_limit", 0.55))
    hard_risk = (
        (hard_margin > 0 and margin_load >= hard_margin)
        or bool(self._mgn_ratio_opening_block_reason())
    )
    if not hard_risk:
        self._over_cap_unwind_last_reason = "soft_capacity_only"
        return None
    ct_val = max(0.0, safe_float(getattr(self, "_ct_val", 0.0)))
    if ct_val <= 0:
        return None
    limits = self._dynamic_notional_limits()
    side_cap = max(0.0, safe_float(limits.get("side_cap", 0.0)))
    long_cap = max(0.0, safe_float(limits.get("long_cap", side_cap)))
    short_cap = max(0.0, safe_float(limits.get("short_cap", side_cap)))
    total_cap = max(0.0, safe_float(limits.get("total_cap", 0.0)))
    unit_notional = price * ct_val
    long_notional = max(0.0, self._long.qty) * unit_notional
    short_notional = max(0.0, self._short.qty) * unit_notional
    gross_notional = long_notional + short_notional
    side_over = {
        "long": max(0.0, long_notional - long_cap) if long_cap > 0 else 0.0,
        "short": max(0.0, short_notional - short_cap) if short_cap > 0 else 0.0,
    }
    gross_over = max(0.0, gross_notional - total_cap) if total_cap > 0 else 0.0
    triggered = any(value > max(0.01, unit_notional) for value in side_over.values()) or (
        gross_over > max(0.01, unit_notional)
    )
    if not triggered:
        if getattr(self, "_over_cap_unwind_mode", False):
            self.log.info(
                f"[over_cap_unwind] {self._symbol} exited: "
                f"long={long_notional:.2f} short={short_notional:.2f} gross={gross_notional:.2f}"
            )
        self._over_cap_unwind_mode = False
        self._over_cap_unwind_last_reason = ""
        return None

    self._over_cap_unwind_mode = True
    dominant = "long" if side_over["long"] >= side_over["short"] else "short"
    if side_over[dominant] <= 0:
        dominant = "long" if long_notional >= short_notional else "short"
    pos = self._long if dominant == "long" else self._short
    if pos.qty <= self._position_qty_tolerance():
        other = "short" if dominant == "long" else "long"
        if (self._short if other == "short" else self._long).qty > self._position_qty_tolerance():
            dominant = other
            pos = self._short if other == "short" else self._long
    if pos.qty <= self._position_qty_tolerance():
        self._over_cap_unwind_last_reason = "no_position"
        return {"type": "skip", "reason": "over_cap_unwind_no_position"}

    now = time.time()
    cooldown = max(30.0, safe_float(getattr(self, "_over_cap_unwind_cooldown_sec", 60.0)))
    remaining = cooldown - (now - safe_float(getattr(self, "_last_over_cap_unwind_ts", 0.0)))
    self._over_cap_unwind_last_reason = f"{dominant}:cooldown" if remaining > 0 else dominant
    if remaining > 0:
        return {"type": "skip", "reason": "over_cap_unwind_cooldown", "cooldown_left": round(remaining, 1)}

    target_ratio = min(0.98, max(0.70, safe_float(getattr(self, "_over_cap_unwind_target_ratio", 0.90))))
    target_side = (long_cap if dominant == "long" else short_cap) * target_ratio
    dominant_notional = long_notional if dominant == "long" else short_notional
    required_notional = max(
        0.0,
        dominant_notional - target_side,
        gross_notional - total_cap * target_ratio if total_cap > 0 else 0.0,
    )
    stage_ratio = min(0.50, max(0.05, safe_float(getattr(self, "_over_cap_unwind_stage_ratio", 0.15))))
    batch_notional = required_notional * stage_ratio
    min_notional = max(0.0, safe_float(getattr(self, "_over_cap_unwind_min_notional_usdt", 6.0)))
    max_notional = max(0.0, safe_float(getattr(self, "_over_cap_unwind_max_notional_usdt", 36.0)))
    batch_notional = max(batch_notional, min_notional)
    if max_notional > 0:
        batch_notional = min(batch_notional, max_notional)
    batch_notional = min(batch_notional, max(0.0, dominant_notional))
    qty = math.floor((batch_notional / unit_notional) / max(self._qty_step, 1e-12)) * self._qty_step
    qty = min(max(0.0, qty), max(0.0, pos.qty))
    if qty + self._position_qty_tolerance() < self._min_qty:
        self._over_cap_unwind_last_reason = "below_min_qty"
        return {"type": "skip", "reason": "over_cap_unwind_below_min_qty"}

    close_side = "sell" if dominant == "long" else "buy"
    before_qty = pos.qty
    self._cancel_opening_orders()
    self._last_over_cap_unwind_ts = now
    result = self._submit_order(
        self._symbol,
        close_side,
        str(round(qty / max(self._qty_step, 1e-12)) * self._qty_step),
        ord_type="market",
        pos_side=dominant,
        reduce_only=True,
    )
    order_id = self._extract_order_id(result)
    code = self._order_error_code(result)
    if not self._order_succeeded(result):
        refresh = self._refresh_position_after_reduce(dominant)
        self._over_cap_unwind_last_result = f"rejected:{code or 'unknown'}"
        self._record_event(
            "over_cap_unwind", dominant, "rejected", detail=f"code={code or 'unknown'}",
            price=price, qty=qty, dedupe_sec=30.0,
        )
        self.log.warning(
            f"[over_cap_unwind] {self._symbol} {dominant} rejected code={code or 'unknown'} "
            f"qty={qty:.4f} refresh={refresh} result={result}"
        )
        return {"type": "skip", "reason": "over_cap_unwind_rejected", "code": code, "result": result}

    refresh = self._refresh_position_after_reduce(dominant)
    after_qty = safe_float(refresh.get("qty")) if refresh.get("confirmed") else before_qty
    reduced_qty = max(0.0, before_qty - after_qty)
    if not refresh.get("confirmed"):
        self._over_cap_unwind_last_result = "accepted_unconfirmed"
        self._record_event("over_cap_unwind", dominant, "accepted_unconfirmed", price=price, qty=qty)
        self.log.warning(
            f"[over_cap_unwind] {self._symbol} {dominant} accepted but position unconfirmed "
            f"ordId={order_id} qty={qty:.4f} refresh={refresh}"
        )
    elif reduced_qty <= self._position_qty_tolerance():
        self._over_cap_unwind_last_result = "accepted_no_reduction"
        self._record_event("over_cap_unwind", dominant, "accepted_no_reduction", price=price, qty=qty)
        self.log.warning(
            f"[over_cap_unwind] {self._symbol} {dominant} accepted without observed reduction "
            f"ordId={order_id} before={before_qty:.4f} after={after_qty:.4f}"
        )
    else:
        self._over_cap_unwind_last_result = "reduced"
        self._record_event(
            "over_cap_unwind", dominant, "reduced", detail=f"ordId={order_id}",
            price=price, qty=reduced_qty, dedupe_sec=5.0,
        )
        self.log.warning(
            f"[over_cap_unwind] {self._symbol} {dominant} reduced "
            f"{reduced_qty:.4f} contracts ordId={order_id}"
        )
        self._tp_init_done = False
        self._init_existing_tp(cancel_existing=False)
    return {
        "type": "over_cap_unwind",
        "side": dominant,
        "action": close_side,
        "quantity": str(qty),
        "pos_side": dominant,
        "reduce_only": True,
        "order_id": order_id,
        "reduced_quantity": round(reduced_qty, 8),
        "reason": "capacity_exhausted",
    }
