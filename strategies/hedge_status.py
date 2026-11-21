"""Read-only status assembly extracted from FuturesHedgeStrategy."""
from typing import Dict
import time
from utils.helpers import safe_float

def _runtime_summary(self) -> Dict:
    return {"strategy": "futures_hedge", "symbol": self._symbol, "running": self._running, "init": self._init_done, "halted": self._trading_halted, "halt_reason": self._halt_reason}

def _position_summary(self) -> Dict:
    return {"long_qty": round(self._long.qty, 4), "short_qty": round(self._short.qty, 4), "long_adds": self._long.adds, "short_adds": self._short.adds}

def _tp_summary(self) -> Dict:
    return {"tp_active": self._active_tp_orders, "tp_max": self._max_tp_orders}

def build_status(self) -> Dict:
    imbalance_ratio = self._net_imbalance_ratio()
    dominant_side = self._dominant_side()
    startup_remaining = 0.0
    if self._startup_recovery_active and self._startup_recovery_seconds > 0:
        startup_remaining = max(0.0, self._startup_recovery_seconds - (time.time() - self._startup_sync_ts))
    recovery_remaining = self._session_recovery_remaining_sec()
    account_margin_load = self._account_margin_load_ratio()
    strategy_notional_or_cost_ratio = self._strategy_notional_or_cost_ratio()
    notional_limits = self._dynamic_notional_limits()
    inventory_repair_plan = self._inventory_repair_plan(self._last_price) if self._last_price > 0 else {}
    inventory_repair = dict(inventory_repair_plan or {})
    if self._last_price <= 0:
        inventory_repair_reason = "no_price"
    elif inventory_repair.get("active"):
        inventory_repair_reason = "repair_needed"
    elif inventory_repair.get("exit_ready"):
        inventory_repair_reason = "balanced"
    elif inventory_repair.get("entry_triggered"):
        inventory_repair_reason = "repair_pending"
    else:
        inventory_repair_reason = "not_needed"
    opening_pause = self.opening_pause_status()
    liquidation = dict(getattr(self, "_liquidation_status", {}) or {})
    liquidation["opening_pause_active"] = opening_pause["active"]
    liquidation["opening_pause_reason"] = opening_pause["reason"]
    liquidation["opening_pause_remaining_sec"] = round(opening_pause["remaining_sec"], 1)
    order_tracker_summary = {}
    try:
        tracker = getattr(self, "_order_tracker", None)
        if tracker:
            order_tracker_summary = tracker.summary()
    except Exception:
        order_tracker_summary = {}
    tp_coverage = {
        "long": self._risk_tp_covered_qty("long"),
        "short": self._risk_tp_covered_qty("short"),
    }
    tp_tolerance = self._position_qty_tolerance()
    tp_coverage_source = (
        "exchange_live_orders"
        if bool(getattr(self, "_exchange_tp_coverage_ready", False))
        else "strategy_runtime"
    )
    return {
        "strategy":"futures_hedge","symbol":self._symbol,
        "profile_slug":self._profile_slug,
        "profile_name":self._profile_name,
        "is_primary_profile":self._is_primary_profile,
        "running":self._running,"init":self._init_done,
        "reduce_only":bool(getattr(self, "_reduce_only_mode", False)),
        "halted":self._trading_halted,"halt_reason":self._halt_reason,
        "halt_updated_at":self._halt_updated_at,
        "resume_required":bool(getattr(self, "_resume_required", False)),
        "halt_operator":str(getattr(self, "_halt_operator", "") or ""),
        "liquidation":liquidation,
        "lev":self._cur_lev,
        "configured_leverage":int(round(safe_float(getattr(self, "_base_lev", 0)))),
        "exchange_leverage":dict(getattr(self, "_exchange_leverage_by_side", {})),
        "leverage_mismatch":any(
            safe_float(value) > 0
            and int(round(safe_float(value))) != int(round(safe_float(getattr(self, "_base_lev", 0))))
            for value in getattr(self, "_exchange_leverage_by_side", {}).values()
        ),
        "breaker":time.time() < self._breaker_until,
        "tp_active":self._active_tp_orders,"tp_max":self._max_tp_orders,
        "long_qty":round(self._long.qty,4),"long_price":round(self._long.entry,2),
        "long_adds":self._long.adds,"max_long":self._max_long_pos,
        "short_qty":round(self._short.qty,4),"short_price":round(self._short.entry,2),
        "short_adds":self._short.adds,"max_short":self._max_short_pos,
        "pnl":round(self._total_pnl,2),"peak":round(self._peak_pnl,4),
        "session_pnl":round(self._session_realized_pnl(),2),
        "daily_pnl":round(self._daily_realized_pnl(),2),
        "total_fees":round(self._total_fees,4),
        "cached_avail":round(self._cached_avail,2),
        "cached_equity":round(self._cached_equity,2),
        "risk_equity_baseline":round(self._risk_equity_baseline,2),
        "day_equity_baseline":round(self._day_equity_baseline,2),
        "recovery_mode":self._in_recovery_mode(),
        "effective_base_pos_usdt":round(self._effective_base_pos_usdt(),2),
        "daily_stop_active":self._daily_stop_until > time.time(),
        "daily_stop_reason":self._daily_stop_reason,
        "daily_stop_latched":bool(getattr(self, "_daily_stop_latched", False)),
        "fee_pnl_file":self._fee_pnl_file,
        "halt_state_file":self._halt_state_file,
        "pnl_csv_file":self._pnl_csv_path,
        "segments_long":len(self._segments["long"]),
        "segments_short":len(self._segments["short"]),
        "monitor":{
            "price":round(self._last_price, 6),
            "grid_atr_enabled":bool(getattr(self, "_grid_atr_enabled", False)),
            "grid_atr_ratio":round(safe_float(getattr(self, "_moca_atr_ratio", 0.0)), 6),
            "grid_effective_step_long":round(self._effective_side_step("long"), 6),
            "grid_effective_step_short":round(self._effective_side_step("short"), 6),
            "grid_range_enabled":bool(getattr(self, "_grid_range_enabled", False)),
            "grid_lower_price":round(safe_float(getattr(self, "_grid_lower_price", 0.0)), 6),
            "grid_upper_price":round(safe_float(getattr(self, "_grid_upper_price", 0.0)), 6),
            "grid_boundary_action":str(getattr(self, "_grid_boundary_action", "pause")),
            "grid_range_long_block_reason":self._grid_range_side_block_reason("long"),
            "grid_range_short_block_reason":self._grid_range_side_block_reason("short"),
            "long_notional":round(self._long.qty * self._last_price * self._ct_val, 2) if self._last_price > 0 else 0.0,
            "short_notional":round(self._short.qty * self._last_price * self._ct_val, 2) if self._last_price > 0 else 0.0,
            "net_exposure_units":round(self._long.qty - self._short.qty, 4),
            "net_exposure_usdt":round((self._long.qty - self._short.qty) * self._last_price * self._ct_val, 2) if self._last_price > 0 else 0.0,
            "net_notional_soft_limit_usdt":round(safe_float(getattr(self, "_max_net_notional_soft_usdt", 0.0)), 2),
            "net_notional_hard_limit_usdt":round(safe_float(getattr(self, "_max_net_notional_usdt", 0.0)), 2),
            "inventory_net_size_multiplier_long":round(self._inventory_net_size_multiplier("long"), 4),
            "inventory_net_size_multiplier_short":round(self._inventory_net_size_multiplier("short"), 4),
            "inventory_hard_hedge_enabled":bool(getattr(self, "_inventory_hard_hedge_enabled", False)),
            "inventory_hard_breach_count":int(getattr(self, "_inventory_hard_breach_count", 0)),
            "inventory_hard_hedge_cooldown_sec":round(safe_float(getattr(self, "_inventory_hard_hedge_cooldown_sec", 0.0)), 2),
            "inventory_hard_hedge_max_notional_usdt":round(safe_float(getattr(self, "_inventory_hard_hedge_max_notional_usdt", 0.0)), 2),
            "long_position_age_sec":round(self._position_age_sec("long"), 1),
            "short_position_age_sec":round(self._position_age_sec("short"), 1),
            "long_unrealized_pnl":round(self._position_unrealized_pnl("long"), 4),
            "short_unrealized_pnl":round(self._position_unrealized_pnl("short"), 4),
            "total_unrealized_pnl":round(self._position_unrealized_pnl("long") + self._position_unrealized_pnl("short"), 4),
            "floating_loss_size_ratio":round(safe_float(getattr(self, "_floating_loss_ratio", 1.0)), 4),
            "floating_loss_freeze_active":bool(getattr(self, "_floating_loss_freeze_active", False)),
            "long_inventory_age_multiplier":round(self._inventory_age_multiplier("long"), 4),
            "short_inventory_age_multiplier":round(self._inventory_age_multiplier("short"), 4),
             "base_side_notional_cap_usdt":round(self._max_side_notional_usdt, 2),
             "base_long_side_notional_cap_usdt":round(self._max_long_side_notional_usdt or self._max_side_notional_usdt, 2),
             "base_short_side_notional_cap_usdt":round(self._max_short_side_notional_usdt or self._max_side_notional_usdt, 2),
             "base_total_notional_cap_usdt":round(self._max_total_notional_usdt or (self._max_long_side_notional_usdt or self._max_side_notional_usdt) + (self._max_short_side_notional_usdt or self._max_side_notional_usdt), 2),
             "effective_side_notional_cap_usdt":round(notional_limits["side_cap"], 2),
             "effective_long_side_notional_cap_usdt":round(notional_limits["long_cap"], 2),
             "effective_short_side_notional_cap_usdt":round(notional_limits["short_cap"], 2),
             "effective_total_notional_cap_usdt":round(notional_limits["total_cap"], 2),
             "effective_working_total_notional_cap_usdt":round(notional_limits["total_cap"] * safe_float(getattr(self, "_working_total_notional_ratio", 1.0)), 2),
             "notional_cap_scale":round(notional_limits.get("scale", 1.0), 4),
             "notional_cap_scaled_total_usdt":round(notional_limits.get("scaled_total_cap", notional_limits["total_cap"]), 2),
             "notional_cap_margin_budget_usdt":round(notional_limits.get("margin_budget_usdt", 0.0), 2),
             "notional_cap_margin_based_total_usdt":round(notional_limits.get("margin_based_total_cap", 0.0), 2),
             "notional_cap_headroom_usdt":round(
                 max(0.0, notional_limits["total_cap"] - (
                     self._long.qty + self._short.qty
                 ) * self._last_price * self._ct_val),
                 2,
             ),
             "notional_cap_margin_health":round(notional_limits.get("margin_health", 0.0), 4),
             "notional_cap_net_ratio":round(notional_limits.get("net_ratio", 0.0), 4),
            "active_inventory_repair_enabled":bool(getattr(self, "_active_inventory_repair_enabled", False)),
            "inventory_repair_mode":bool(getattr(self, "_inventory_repair_mode", False)),
            "inventory_repair_active":bool(inventory_repair_plan.get("active", False)),
            "inventory_repair_side":str(inventory_repair_plan.get("repair_side", "") or ""),
            "inventory_repair_proposed_qty":round(safe_float(inventory_repair_plan.get("proposed_qty")), 4),
            "inventory_repair_net_notional":round(safe_float(inventory_repair_plan.get("net_notional")), 2),
            "exchange_long_leverage":int(round(safe_float(getattr(self, "_exchange_leverage_by_side", {}).get("long", 0)))),
            "exchange_short_leverage":int(round(safe_float(getattr(self, "_exchange_leverage_by_side", {}).get("short", 0)))),
            "exchange_long_imr_usdt":round(safe_float(getattr(self, "_exchange_position_imr_by_side", {}).get("long", 0.0)), 4),
            "exchange_short_imr_usdt":round(safe_float(getattr(self, "_exchange_position_imr_by_side", {}).get("short", 0.0)), 4),
            "exchange_position_imr_total_usdt":round(sum(safe_float(value) for value in getattr(self, "_exchange_position_imr_by_side", {}).values()), 4),
            "inventory_repair_exit_net_notional_usdt":round(safe_float(getattr(self, "_inventory_repair_exit_net_notional_usdt", 0.0)), 2),
            "inventory_repair_effective_exit_net_notional_usdt":round(safe_float(getattr(self, "_inventory_repair_exit_net_notional_usdt", 0.0)) + max(0.0, self._min_qty * self._last_price * self._ct_val), 2),
            "inventory_repair_cooldown_remaining_sec":round(max(0.0, self._inventory_repair_cooldown(safe_float(inventory_repair_plan.get("net_notional"))) - (time.time() - safe_float(getattr(self, "_last_inventory_repair_ts", 0.0)))), 1),
            "over_cap_unwind_enabled":bool(getattr(self, "_over_cap_unwind_enabled", False)),
            "over_cap_unwind_mode":bool(getattr(self, "_over_cap_unwind_mode", False)),
            "over_cap_unwind_last_reason":str(getattr(self, "_over_cap_unwind_last_reason", "") or ""),
            "over_cap_unwind_last_result":str(getattr(self, "_over_cap_unwind_last_result", "") or ""),
            "over_cap_unwind_cooldown_remaining_sec":round(max(0.0, safe_float(getattr(self, "_over_cap_unwind_cooldown_sec", 60.0)) - (time.time() - safe_float(getattr(self, "_last_over_cap_unwind_ts", 0.0)))), 1),
            "inventory_repair_ready":bool(inventory_repair.get("active", False)),
            "inventory_repair_reason":inventory_repair_reason,
            "inventory_repair_imbalance_pct":round(safe_float(inventory_repair.get("imbalance_ratio")) * 100, 2),
            "inventory_repair_trigger_imbalance_pct":round(safe_float(getattr(self, "_inventory_repair_imbalance_gate", 0.70)) * 100, 2),
            "add_rate_limit_window_sec":round(safe_float(getattr(self, "_add_rate_limit_window_sec", 300.0)), 1),
            "regular_add_rate_limit_margin_usdt":round(safe_float(getattr(self, "_regular_add_rate_limit_margin_usdt", 15.0)), 2),
            "inventory_repair_rate_limit_margin_usdt":round(safe_float(getattr(self, "_inventory_repair_rate_limit_margin_usdt", 45.0)), 2),
            "paired_add_rate_limit_margin_usdt":round(safe_float(getattr(self, "_paired_add_rate_limit_margin_usdt", 45.0)), 2),
            "inventory_margin_paused":bool(getattr(self, "_inventory_margin_paused", False)),
            "inventory_margin_block_long":bool(self._inventory_opening_margin_blocked("long")),
            "inventory_margin_block_short":bool(self._inventory_opening_margin_blocked("short")),
            "side_config":{
                "long":dict(getattr(self, "_side_config", {}).get("long", {}) or {}),
                "short":dict(getattr(self, "_side_config", {}).get("short", {}) or {}),
            },
            "paired_add_enabled":bool(getattr(self, "_paired_add_enabled", False)),
            "paired_add_cooldown_remaining_sec":round(max(0.0, safe_float(getattr(self, "_paired_add_cooldown_sec", 120)) - (time.time() - safe_float(getattr(self, "_last_paired_add_ts", 0.0)))), 1),
            "paired_add_trigger_ratio":round(safe_float(getattr(self, "_paired_add_trigger_ratio", 0.0075)), 4),
            "paired_add_anchor_price":round(safe_float(getattr(self, "_paired_add_anchor_price", 0.0)), 6),
            "conditional_missing_side_repair_enabled":bool(getattr(self, "_conditional_missing_side_repair_enabled", False)),
            "missing_side_repair_allow_legacy_state":bool(getattr(self, "_missing_side_repair_allow_legacy_state", False)),
            "missing_side_repair_imbalance_threshold":round(safe_float(getattr(self, "_missing_side_repair_imbalance_threshold", 0.70)), 4),
            "missing_side_repair_cooldown_remaining_sec":round(max(0.0, safe_float(getattr(self, "_missing_side_repair_cooldown_sec", 180.0)) - (time.time() - safe_float(getattr(self, "_last_missing_side_repair_ts", 0.0)))), 1),
            "missing_side_repair_last_reason":str(getattr(self, "_missing_side_repair_last_reason", "") or ""),
            "normal_close_sides":dict(getattr(self, "_normal_close_sides", {})),
            "risk_opening_pause_active":opening_pause["active"],
            "risk_opening_pause_reason":opening_pause["reason"],
            "risk_opening_pause_remaining_sec":round(opening_pause["remaining_sec"], 1),
            "liq_px_unavailable":bool(liquidation.get("liq_px_unavailable", False)),
            "liquidation_distance_safe":bool(liquidation.get("liquidation_distance_safe", False)),
            "liquidation_warning":bool(liquidation.get("liquidation_warning", False)),
            "liquidation_emergency":bool(liquidation.get("liquidation_emergency", False)),
            "liquidation_zone":str(liquidation.get("zone", "unavailable") or "unavailable"),
            "liquidation_reason":str(liquidation.get("reason", "") or ""),
            "liquidation_min_distance_rate":liquidation.get("min_distance_rate"),
            "liquidation_mark_px":liquidation.get("mark_px"),
            "liquidation_opening_pause_remaining_sec":round(opening_pause["remaining_sec"], 1),
            "imbalance_ratio":round(imbalance_ratio, 4),
            "imbalance_pct":round(imbalance_ratio * 100, 2),
            "dominant_side":dominant_side or "balanced",
            "startup_recovery_active":False,
            "startup_recovery_remaining_sec":0.0,
            "post_recovery_refill_delay_active":self._in_post_recovery_refill_delay(),
            "recovery_mode":self._in_recovery_mode(),
            "recovery_remaining_sec":round(recovery_remaining, 1),
            "target_grid_layers":self._target_grid_layers(),
            "grid_layer_limit_reason":self._grid_layer_limit_reason(),
            "grid_long_enabled":self._grid_side_enabled("long"),
            "grid_short_enabled":self._grid_side_enabled("short"),
            "grid_long_block_reason":self._grid_side_block_reason("long"),
            "grid_short_block_reason":self._grid_side_block_reason("short"),
            "grid_long_count":len(self._long_grid_levels),
            "grid_short_count":len(self._short_grid_levels),
            "grid_refill_long_block_reason":self._grid_refill_block_reason("long"),
            "grid_refill_short_block_reason":self._grid_refill_block_reason("short"),
            "opening_grid_long_count":len(self._long_grid_levels),
            "opening_grid_short_count":len(self._short_grid_levels),
            "tp_order_count":self._active_tp_orders,
            "tp_coverage_source":tp_coverage_source,
            "tp_coverage_age_sec":round(max(0.0, time.time() - safe_float(getattr(self, "_exchange_tp_coverage_at", 0.0))), 1) if tp_coverage_source == "exchange_live_orders" else None,
            "tp_long_covered_qty":round(tp_coverage["long"], 4),
            "tp_short_covered_qty":round(tp_coverage["short"], 4),
            "tp_long_coverage_complete":self._long.qty <= 0 or tp_coverage["long"] + tp_tolerance >= self._long.qty,
            "tp_short_coverage_complete":self._short.qty <= 0 or tp_coverage["short"] + tp_tolerance >= self._short.qty,
            "order_tracker":order_tracker_summary,
            "order_active_avg_age_sec":order_tracker_summary.get("active_avg_age_sec", 0.0),
            "order_active_max_age_sec":order_tracker_summary.get("active_max_age_sec", 0.0),
            "grid_order_max_age_sec":order_tracker_summary.get("grid_max_age_sec", 0.0),
            "tp_order_max_age_sec":order_tracker_summary.get("tp_max_age_sec", 0.0),
            "cached_avail":round(self._cached_avail, 2),
                    "cached_frozen":round(self._cached_frozen, 2),
                    "cached_equity":round(self._cached_equity, 2),
                    "account_risk":dict(self._account_risk_snapshot),
                    "margin_load_ratio":round(account_margin_load, 4),
            "margin_load_pct":round(account_margin_load * 100, 2),
            "account_margin_load_ratio":round(account_margin_load, 4),
            "account_margin_load_pct":round(account_margin_load * 100, 2),
            "account_mgn_ratio":round(self._account_mgn_ratio(), 4),
            "account_risk_age_sec":(
                round(max(0.0, time.time() - safe_float((self._account_risk_snapshot or {}).get("observed_at_ms")) / 1000.0), 1)
                if safe_float((self._account_risk_snapshot or {}).get("observed_at_ms")) > 0 else None
            ),
            "account_risk_data_block_reason":self._account_margin_data_block_reason(),
            "mgn_ratio_open_limit":round(safe_float(getattr(self, "_mgn_ratio_open_limit", 5.0)), 4),
            "mgn_ratio_opening_block_reason":self._mgn_ratio_opening_block_reason(),
            "account_budget":self._account_budget_snapshot(),
            "opening_guard_stats":self._opening_guard_stats(),
            "inventory_margin_hedge_ratio":round(self._inventory_margin_thresholds()[2], 4),
            "inventory_margin_effective_stop_ratio":round(self._inventory_margin_thresholds()[0], 4),
            "inventory_margin_effective_resume_ratio":round(self._inventory_margin_thresholds()[1], 4),
            "strategy_notional_or_cost_ratio":round(strategy_notional_or_cost_ratio, 4),
            "strategy_notional_or_cost_pct":round(strategy_notional_or_cost_ratio * 100, 2),
            "strategy_cost_ratio":round(strategy_notional_or_cost_ratio, 4),
            "strategy_cost_pct":round(strategy_notional_or_cost_ratio * 100, 2),
            "effective_base_pos_usdt":round(self._effective_base_pos_usdt(), 2),
            "grid_bias_ratio":round(self._grid_bias_ratio, 4),
            "moca_apply_live":self._moca_apply_live,
            "moca_observed_state":self._moca_observed_state or "unknown",
            "moca_observed_confidence":round(self._moca_observed_confidence, 4),
            "moca_atr_ratio":round(safe_float(getattr(self, "_moca_atr_ratio", 0.0)), 6),
            "moca_grid_step_multiplier":round(safe_float(getattr(self, "_moca_grid_step_multiplier", 1.0)), 4),
            "moca_effective_add_step":round(safe_float(getattr(self, "_add_step", 0.0)), 6),
            # Deprecated aliases retained for one API release. These are
            # derived from inventory repair state and no longer drive risk.
            "rebalance_ready":bool(inventory_repair.get("active", False)),
            "rebalance_reason":inventory_repair_reason,
            "rebalance_cooldown_sec":round(max(0.0, safe_float(getattr(self, "_inventory_repair_cooldown_sec", 120.0)) - (time.time() - safe_float(getattr(self, "_last_inventory_repair_ts", 0.0)))), 1),
            "rebalance_trigger_imbalance_pct":round(safe_float(getattr(self, "_inventory_repair_imbalance_gate", 0.70)) * 100, 2),
            "global_add_block_reason":self._global_add_block_reason,
            "global_add_cooldown_sec":round(self._global_add_cooldown_left, 1),
            "long_add_trigger_price":round(self._long_add_trigger_price, 6) if self._long_add_trigger_price > 0 else 0.0,
            "short_add_trigger_price":round(self._short_add_trigger_price, 6) if self._short_add_trigger_price > 0 else 0.0,
            "long_add_gap_pct":round(((self._long_add_trigger_price - self._last_price) / self._last_price) * 100, 2) if self._last_price > 0 and self._long_add_trigger_price > 0 else 0.0,
            "short_add_gap_pct":round(((self._short_add_trigger_price - self._last_price) / self._last_price) * 100, 2) if self._last_price > 0 and self._short_add_trigger_price > 0 else 0.0,
            "long_add_block_reason":self._long_add_block_reason,
            "short_add_block_reason":self._short_add_block_reason,
            "long_add_cooldown_sec":round(self._long_add_cooldown_left, 1),
            "short_add_cooldown_sec":round(self._short_add_cooldown_left, 1),
            "last_event_type":self._last_event_type,
            "last_event_side":self._last_event_side,
            "last_event_reason":self._last_event_reason,
            "last_event_detail":self._last_event_detail,
            "last_event_price":round(self._last_event_price, 6) if self._last_event_price > 0 else 0.0,
            "last_event_qty":round(self._last_event_qty, 4),
            "last_event_at":int(self._last_event_ts) if self._last_event_ts > 0 else 0,
        },
    }
