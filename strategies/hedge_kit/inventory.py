"""库存修复与配对加仓。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class InventoryMixin:
    """库存修复与配对加仓（19 个方法）。"""

    def _inventory_net_size_multiplier(self, side: str) -> float:
        return inventory_net_size_multiplier(
            side,
            self._dominant_side(),
            self._net_notional_exposure(),
            safe_float(getattr(self, "_max_net_notional_soft_usdt", 0.0)),
            safe_float(getattr(self, "_max_net_notional_usdt", 0.0)),
        )

    def _maybe_repair_hard_net_exposure(self, price: float) -> Optional[dict]:
        if not bool(getattr(self, "_inventory_hard_hedge_enabled", False)):
            return None
        if self._trading_halted or getattr(self, "_reduce_only_mode", False):
            return None
        if not self._client or price <= 0:
            return None
        hard = safe_float(getattr(self, "_max_net_notional_usdt", 0.0))
        net_notional = self._net_notional_exposure(price)
        if hard <= 0 or net_notional <= hard:
            self._inventory_hard_breach_count = 0
            return None
        self._inventory_hard_breach_count += 1
        confirmations = max(
            1, int(getattr(self, "_inventory_hard_hedge_confirmations", 2))
        )
        if self._inventory_hard_breach_count < confirmations:
            return None
        now = time.time()
        cooldown = max(
            3.0, safe_float(getattr(self, "_inventory_hard_hedge_cooldown_sec", 60.0))
        )
        if now - safe_float(getattr(self, "_last_inventory_hard_hedge_ts", 0.0)) < cooldown:
            return None
        net_units = safe_float(self._long.qty) - safe_float(self._short.qty)
        weak_side = "short" if net_units > 0 else "long"
        action = "sell" if weak_side == "short" else "buy"
        qty = hard_hedge_order_qty(
            net_notional,
            safe_float(getattr(self, "_max_net_notional_soft_usdt", 0.0)),
            hard,
            price,
            safe_float(getattr(self, "_ct_val", 0.0)),
            safe_float(getattr(self, "_qty_step", 0.0)),
            safe_float(getattr(self, "_min_qty", 0.0)),
            safe_float(getattr(self, "_inventory_hard_hedge_max_notional_usdt", 0.0)),
        )
        if qty <= 0:
            return None
        result = self._place_open_order(action, qty, weak_side, inventory_repair=True)
        if not self._order_succeeded(result):
            self.log.warning(
                f"[inventory_hedge] hard breach hedge failed side={weak_side} "
                f"qty={qty:.8f} net={net_notional:.2f} result={result}"
            )
            return None
        if weak_side == "short":
            self._short.qty += qty
        else:
            self._long.qty += qty
        self._last_inventory_hard_hedge_ts = now
        self._inventory_hard_breach_count = 0
        self._record_event(
            "inventory_hedge",
            weak_side,
            "hard_net_hedge",
            detail=f"net={net_notional:.2f} excess_to_soft={net_notional - safe_float(getattr(self, '_max_net_notional_soft_usdt', 0.0)):.2f}",
            price=price,
            qty=qty,
            dedupe_sec=30.0,
        )
        self.log.warning(
            f"[inventory_hedge] {self._symbol} hard net={net_notional:.2f} "
            f"-> add {weak_side} {qty:.8f} @ {price:.6f}"
        )
        self._init_existing_tp(cancel_existing=False)
        return {
            "type": "inventory_hedge",
            "side": weak_side,
            "action": action,
            "quantity": str(qty),
            "net_notional": net_notional,
        }

    def _maybe_repair_normal_close_missing_side(self, price: float):
        from strategies.hedge_inventory_repair import maybe_repair_normal_close_missing_side
        return maybe_repair_normal_close_missing_side(self, price)

    @property
    def _inventory_or_create(self):
        inventory = getattr(self, "_inventory", None)
        if inventory is None:
            inventory = HedgeInventory()
            self._inventory = inventory
        return inventory

    def _inventory_age_multiplier(self, side: str) -> float:
        if not bool(getattr(self, "_inventory_age_sizing_enabled", True)):
            return 1.0
        age_hours = self._position_age_sec(side) / 3600.0
        loss = -self._position_unrealized_pnl(side)
        if age_hours <= 0 or loss <= 0:
            return 1.0
        age_intensity = min(1.0, age_hours / max(self._inventory_age_hours, 0.1))
        loss_intensity = min(1.0, loss / max(self._inventory_loss_usdt, 0.1))
        intensity = age_intensity * loss_intensity
        return max(0.35, 1.0 - self._inventory_age_reduce_strength * intensity)

    def _inventory_repair_plan(self, price: float, update_mode: bool = False) -> dict:
        from strategies.hedge_risk import inventory_repair_plan
        plan = inventory_repair_plan(
            self._inventory_or_create,
            price,
            self._ct_val,
            self._max_net_notional_usdt,
            safe_float(getattr(self, "_inventory_repair_imbalance_gate", 0.70)),
            safe_float(getattr(self, "_inventory_repair_stage_ratio", 0.30)),
            bool(getattr(self, "_inventory_repair_mode", False)),
            safe_float(
                getattr(self, "_inventory_repair_exit_net_notional_usdt", self._max_net_notional_usdt * 0.8)
            ),
            safe_float(getattr(self, "_inventory_repair_exit_imbalance_ratio", 0.45)),
            safe_float(getattr(self, "_min_qty", 0.0)),
        )
        if update_mode:
            self._inventory_repair_mode = bool(plan.get("active", False))
            if self._inventory_repair_mode:
                self._paired_add_anchor_price = 0.0
                self._paired_add_anchor_side = ""
        return plan

    def _inventory_repair_cooldown(self, net_notional: float) -> float:
        from strategies.hedge_risk import inventory_repair_cooldown
        return inventory_repair_cooldown(
            net_notional,
            safe_float(getattr(self, "_max_net_notional_usdt", 0.0)),
            safe_float(getattr(self, "_inventory_repair_fast_cooldown_sec", 30.0)),
            safe_float(getattr(self, "_inventory_repair_medium_cooldown_sec", 60.0)),
            safe_float(getattr(self, "_inventory_repair_cooldown_sec", 120.0)),
        )

    def _inventory_margin_thresholds(self) -> tuple[float, float, float]:
        from strategies.hedge_risk import inventory_margin_thresholds
        return inventory_margin_thresholds(
            safe_float(getattr(self._long, "qty", 0.0)),
            safe_float(getattr(self._short, "qty", 0.0)),
            safe_float(getattr(self, "_inventory_margin_stop_ratio", 0.35)),
            safe_float(getattr(self, "_max_margin_ratio", 0.35)),
            safe_float(getattr(self, "_account_margin_soft_limit", 0.35)),
            safe_float(getattr(self, "_inventory_margin_hedge_credit_ratio", 0.20)),
            safe_float(getattr(self, "_inventory_margin_resume_ratio", 0.30)),
        )

    def _inventory_opening_margin_blocked(self, side: str = "") -> bool:
        # Compatibility/status hook using the same account semantics as
        # RiskPolicy.  The soft line throttles capacity; only the account hard
        # line, stale margin data, or dangerous mgnRatio can veto an opening.
        if not (
            bool(getattr(self, "_active_inventory_repair_enabled", False))
            or bool(getattr(self, "_paired_add_enabled", False))
        ):
            return False
        margin_load = self._account_margin_load_ratio()
        soft_limit = safe_float(getattr(self, "_account_margin_soft_limit", 0.45))
        hard_limit = safe_float(getattr(self, "_account_margin_hard_limit", soft_limit))
        margin_mode = account_margin_mode(margin_load, soft_limit, hard_limit)
        mgn_reason = self._mgn_ratio_opening_block_reason()
        paused = margin_mode == "HARD" or bool(mgn_reason)
        self._inventory_margin_paused = paused
        return paused

    def _inventory_repair_margin_limit(self) -> float:
        """Use the profile's shared hard margin line for inventory repair."""
        limit = safe_float(getattr(self, "_account_margin_hard_limit", 0.0))
        long_qty = max(0.0, safe_float(getattr(self._long, "qty", 0.0)))
        short_qty = max(0.0, safe_float(getattr(self._short, "qty", 0.0)))
        tolerance = self._position_qty_tolerance()
        missing_side = (
            long_qty + short_qty > tolerance
            and (long_qty <= tolerance or short_qty <= tolerance)
        )
        if missing_side:
            limit = max(
                limit,
                safe_float(getattr(self, "_inventory_repair_missing_side_margin_ratio", 0.0)),
            )
        hard_limit = safe_float(
            getattr(self, "_account_margin_hard_limit", getattr(self, "_hard_margin_ratio", 0.0))
        )
        if hard_limit > 0:
            limit = min(limit, max(0.0, hard_limit - 1e-6))
        return max(0.0, limit)

    def _inventory_repair_hard_override_decision(
        self, repair_side: str, price: float, quantity: float
    ) -> dict:
        """Use the RiskPolicy pure helper for the strategy's early gate."""
        if not bool(getattr(self, "_inventory_repair_hard_override_enabled", False)):
            return {"allowed": False, "reason": "disabled"}
        decision = inventory_repair_hard_margin_override(
            self._risk_context_snapshot(),
            "buy" if repair_side == "long" else "sell",
            repair_side,
            {
                "intent_type": "inventory_repair",
                "inventory_repair": True,
                "quantity": quantity,
                "px": price,
            },
        )
        self._inventory_repair_hard_override_last = dict(decision)
        return decision

    def _inventory_repair_step_qty_cap(
        self, repair_side: str, price: float, desired_qty: float
    ) -> float:
        """Cap the opt-in CRV repair corridor before capacity checks run."""
        from strategies.hedge_grid import inventory_repair_step_qty_cap
        return inventory_repair_step_qty_cap(
            repair_side,
            price,
            desired_qty,
            bool(getattr(self, "_inventory_repair_hard_override_enabled", False)),
            safe_float(getattr(self, "_inventory_repair_hard_override_max_step_notional_usdt", 0.0)),
            safe_float(getattr(self, "_ct_val", 0.0)),
            safe_float(getattr(self, "_qty_step", 0.0)),
        )

    def _inventory_repair_margin_blocked(
        self, repair_side: str = "", price: float = 0.0, quantity: float = 0.0
    ) -> bool:
        """Apply repair-specific gates instead of the normal gross-margin latch."""
        from strategies.hedge_grid import inventory_repair_margin_blocked
        return inventory_repair_margin_blocked(
            bool(getattr(self, "_active_inventory_repair_enabled", False)),
            self._account_margin_data_block_reason,
            self._mgn_ratio_opening_block_reason,
            self._account_margin_load_ratio(),
            self._inventory_repair_margin_limit(),
            self._inventory_repair_hard_override_decision,
            repair_side=repair_side,
            price=price,
            quantity=quantity,
        )

    def _paired_add_margin_allowed(self, dominant_side: str = "") -> bool:
        """Enforce the profile-specific cap for gross-risk paired additions."""
        if not bool(getattr(self, "_paired_add_enabled", False)):
            return False
        dominant_side = str(dominant_side or self._dominant_side()).lower()
        if dominant_side not in {"long", "short"}:
            return False
        margin_load = self._account_margin_load_ratio()
        pair_limit = safe_float(getattr(self, "_paired_add_max_margin_ratio", 0.0))
        if pair_limit > 0.0 and margin_load >= pair_limit:
            return False
        weak_side = "short" if dominant_side == "long" else "long"
        if not self._inventory_opening_margin_blocked(weak_side):
            return True
        return (
            bool(getattr(self, "_paired_add_allow_inventory_pause", False))
            and bool(getattr(self, "_paired_average_down_enabled", False))
            and self._margin_first_capacity_ready()
        )

    def _paired_add_triggered(self, dominant_side: str, price: float) -> bool:
        if not bool(getattr(self, "_paired_add_enabled", False)) or dominant_side not in {"long", "short"}:
            return False
        average_down_mode = bool(getattr(self, "_paired_average_down_enabled", False))
        if bool(getattr(self, "_inventory_repair_mode", False)) and not average_down_mode:
            return False
        trigger = safe_float(getattr(self, "_paired_add_trigger_ratio", 0.0075))
        if average_down_mode:
            # When DRAM is already net-heavy, use the established dominant
            # inventory entry as the adverse-move anchor. This lets a paired
            # add lower the losing side's average without increasing net risk.
            pos = self._long if dominant_side == "long" else self._short
            entry = safe_float(getattr(pos, "entry", 0.0))
            if entry > 0.0:
                if dominant_side == "long":
                    return price <= entry * (1.0 - trigger)
                return price >= entry * (1.0 + trigger)
        if (
            str(getattr(self, "_paired_add_anchor_side", "")) != dominant_side
            or safe_float(getattr(self, "_paired_add_anchor_price", 0.0)) <= 0
        ):
            self._paired_add_anchor_side = dominant_side
            self._paired_add_anchor_price = price
            return False
        if dominant_side == "short":
            return price >= safe_float(self._paired_add_anchor_price) * (1.0 + trigger)
        return price <= safe_float(self._paired_add_anchor_price) * (1.0 - trigger)

    def _paired_add_wick_ok(self, side: str, price: float) -> bool:
        if (
            bool(getattr(self, "_paired_add_ignore_wick", False))
            and bool(getattr(self, "_paired_add_ignore_wick", False))
        ):
            return True
        return self._wick_check(side, price)

    def _pending_repair_qty(self, side: str, price: float) -> float:
        from strategies.hedge_grid import pending_repair_qty
        return pending_repair_qty(
            side,
            price,
            self._effective_side_step,
            self._fetch_live_orders,
        )

    def _maybe_active_inventory_repair(self, price: float):
        from strategies.hedge_inventory_repair import maybe_active_inventory_repair
        return maybe_active_inventory_repair(self, price)

    def _do_paired_add(self, dominant_side: str, price: float):
        if not bool(getattr(self, "_paired_add_enabled", False)) or dominant_side not in {"long", "short"} or price <= 0:
            return None
        now = time.time()
        if now - safe_float(getattr(self, "_last_paired_add_ts", 0.0)) < safe_float(getattr(self, "_paired_add_cooldown_sec", 120)):
            return {"type": "skip", "reason": "paired_add_cooldown"}
        # A net-neutral pair still increases gross margin. Its profile-specific
        # cap is separate from the global account hard line.
        if not self._paired_add_margin_allowed(dominant_side):
            return {"type": "skip", "reason": "paired_add_margin"}
        weak_side = "short" if dominant_side == "long" else "long"
        if (
            not bool(getattr(self, "_paired_add_ignore_layer_cap", False))
            and (self._long_layer >= self._effective_side_max_adds("long") or self._short_layer >= self._effective_side_max_adds("short"))
        ):
            return {"type": "skip", "reason": "paired_add_max_layers"}
        price_value = price * max(self._ct_val, 0.0)
        limits = self._dynamic_notional_limits()
        gross_notional = (self._long.qty + self._short.qty) * price_value
        use_hard_pair_cap = bool(getattr(self, "_paired_add_use_hard_total_cap", False))
        working_total_cap = limits["total_cap"] if use_hard_pair_cap else limits["total_cap"] * safe_float(getattr(self, "_working_total_notional_ratio", 1.0))
        headroom = max(0.0, working_total_cap - gross_notional)
        base_qty = contract_qty_for_margin(
            self._effective_base_pos_usdt(weak_side),
            price,
            self._cur_lev,
            self._ct_val,
            self._qty_step,
        )
        max_pair_qty = math.floor((headroom / max(2.0 * price_value, 1e-8)) / self._qty_step) * self._qty_step
        pair_qty = min(max(self._min_qty, base_qty), max_pair_qty)
        if pair_qty + 1e-12 < self._min_qty:
            return {"type": "skip", "reason": "paired_add_total_cap"}
        # DRAM paired averaging is a market execution path. Existing passive
        # grid opens would otherwise remain counted as pending capacity and
        # reject the weak hedge leg before the neutral pair can be placed.
        # Keep reduce-only TP orders intact; the grid is rebuilt on the next
        # synchronization tick after the pair completes.
        if bool(getattr(self, "_paired_add_enabled", False)) and bool(getattr(self, "_paired_average_down_enabled", False)):
            cancelled = self._cancel_opening_orders()
            if cancelled:
                self.log.info(
                    f"[paired_add] {self._symbol} cancelled {cancelled} stale opening grids"
                )
        before_weak_qty = (self._long if weak_side == "long" else self._short).qty
        weak_result = self._do_add(
            weak_side,
            price,
            max_qty=pair_qty,
            paired_net_neutral=True,
            reason_code="paired_weak",
            allow_hard_total_cap=use_hard_pair_cap,
        )
        if weak_result.get("type") != "add":
            return weak_result
        self._last_paired_add_ts = now
        weak_qty = abs(safe_float(weak_result.get("quantity")))
        dominant_result = self._do_add(
            dominant_side,
            price,
            max_qty=weak_qty,
            paired_net_neutral=True,
            reason_code="paired_dominant",
            allow_hard_total_cap=use_hard_pair_cap,
        )
        if dominant_result.get("type") != "add":
            unwind = self._unwind_failed_pair_leg(
                weak_side,
                before_weak_qty,
                str(weak_result.get("order_id") or ""),
            )
            self._record_event("paired_add_partial", weak_side, "dominant_leg_failed", price=price, qty=weak_qty)
            return {
                "type": "paired_add_partial",
                "weak": weak_result,
                "dominant": dominant_result,
                "unwind": unwind,
            }
        self._paired_add_anchor_price = price
        self._paired_add_anchor_side = dominant_side
        self._record_event("paired_add", dominant_side, "net_neutral", price=price, qty=weak_qty)
        return {"type": "paired_add", "weak": weak_result, "dominant": dominant_result}
