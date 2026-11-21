"""网格铺设、刷新与边界控制。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class GridMixin:
    """网格铺设、刷新与边界控制（18 个方法）。"""

    def _grid_side_enabled(self, side: str) -> bool:
        if side not in {"long", "short"}:
            return False
        if not bool(self._side_value(side, "enabled", True)):
            return False
        if self._side_entry_trigger_blocked(side):
            return False
        if getattr(self, "_reduce_only_mode", False):
            return False
        if self._risk_opening_pause_active():
            return False
        if self._portfolio_throttled_dominant_side_blocked(side):
            return False
        # Inventory margin pause is a side-capacity signal.  It must not turn
        # an imbalanced hedge into a global TP-only lockdown: the weak side
        # may still repair net exposure under the dedicated guards.
        if self._inventory_opening_margin_blocked(side):
            return False
        if self._trading_halted or self._in_startup_recovery() or self._in_post_recovery_refill_delay():
            return False
        if self._grid_pause_active(side):
            return False
        if self._recent_side_loss_active(side):
            return False
        # Trend states should bias, not hard-disable the opposite side.
        return True

    def _hedged_dominant_grid_allowed(self) -> bool:
        # Permit configured DRAM maker refills while actual account margin remains safe.
        return (
            bool(getattr(self, "_hedged_grid_allow_dominant_refill", False))
            and str(getattr(self, "_symbol", "")).upper().startswith("DRAM-")
            and self._margin_first_capacity_ready()
        )

    def _grid_range_side_block_reason(self, side: str) -> str:
        if not bool(getattr(self, "_grid_range_enabled", False)):
            return ""
        side = str(side or "").lower()
        lower = safe_float(getattr(self, "_grid_lower_price", 0.0))
        upper = safe_float(getattr(self, "_grid_upper_price", 0.0))
        price = safe_float(getattr(self, "_last_price", 0.0))
        if side not in {"long", "short"} or lower <= 0 or upper <= lower or price <= 0:
            return ""
        buffer = safe_float(getattr(self, "_grid_boundary_buffer_ratio", 0.002))
        if side == "short" and price >= upper * (1.0 - buffer):
            return "grid_range_upper_boundary"
        if side == "long" and price <= lower * (1.0 + buffer):
            return "grid_range_lower_boundary"
        return ""

    def _grid_capacity_block_reason(self, side: str) -> str:
        """Shared soft capacity checks for grid refill and active adds."""
        if side not in {"long", "short"}:
            return "invalid_side"
        range_block = self._grid_range_side_block_reason(side)
        if range_block:
            return range_block
        dominant = self._dominant_side()
        imbalance = self._net_imbalance_ratio()
        if dominant and side == dominant and self._inventory_net_size_multiplier(side) <= 0:
            return "inventory_net_hard_limit"
        # Net imbalance is observable but is not an ordinary opening veto.
        # Floating loss is a sizing/capacity signal, not a global stop. When
        # the inventory is directional, stop expanding the losing dominant
        # side but keep the weak side available to reduce net exposure.
        if bool(getattr(self, "_floating_loss_freeze_active", False)) and not (
            self._margin_first_capacity_ready()
            and bool(getattr(self, "_margin_first_ignore_floating_loss", False))
        ):
            if dominant and side == dominant and imbalance >= 0.05:
                return "floating_loss_dominant"
            if not dominant and imbalance < 0.05:
                return "floating_loss_hedged"
        if self._recent_side_loss_active(side):
            return "recent_loss_guard"
        if self._cached_equity > 0:
            margin_load = self._account_margin_load_ratio()
            margin_cap = max(
                self._grid_expand_margin_ratio + 0.1,
                self._margin_cap_ratio_for_side(side),
            )
            if margin_load >= margin_cap and not self._small_strategy_margin_bypass(
                side, self._effective_base_pos_usdt()
            ):
                return f"margin_load_{margin_load:.2f}"
        return ""

    def _grid_refill_block_reason(self, side: str) -> str:
        if side not in {"long", "short"}:
            return "invalid_side"
        tp_block = self._tp_coverage_block_reason(side)
        if tp_block:
            return tp_block
        mgn_block = self._mgn_ratio_opening_block_reason()
        if mgn_block:
            return mgn_block
        if not self._grid_side_enabled(side):
            return self._grid_side_block_reason(side) or "grid_side_disabled"
        # Pairing remains optional; a dominant inventory never monopolizes an
        # independent grid side. Capacity and account hard limits still apply.
        return self._grid_capacity_block_reason(side)

    def _target_grid_layers(self) -> int:
        layers = min(
            max(self._effective_side_max_adds("long"), self._effective_side_max_adds("short")),
            max(1, int(getattr(self, "_grid_max_layers", 3) or 3)),
        )
        state = getattr(self, "_moca_state", "")
        if state in {"trending_up", "trending_down"}:
            layers = min(layers, 2)
        elif state == "spike":
            layers = min(layers, 1)
        if self._in_startup_recovery() or self._in_post_recovery_refill_delay():
            layers = min(layers, max(1, self._post_recovery_grid_layers))
        if self._net_imbalance_ratio() >= self._grid_bias_ratio:
            layers = min(layers, 2)
        if self._cached_equity > 0:
            margin_load = self._account_margin_load_ratio()
            if margin_load >= self._grid_expand_margin_ratio:
                layers = min(layers, 1)
        free_avail = self._available_after_reserve()
        if free_avail > 0:
            base_budget = max(1.0, min(self._effective_base_pos_usdt("long"), self._effective_base_pos_usdt("short")))
            layers = min(layers, max(1, int(free_avail / (base_budget * 2))))
        return max(1, layers)

    def _grid_side_block_reason(self, side: str) -> str:
        """Return the first concrete gate that disabled this grid side.

        Order mirrors ``_grid_side_enabled`` so the reported reason is the
        gate that actually failed rather than a generic placeholder.
        """
        if side not in {"long", "short"}:
            return "invalid_side"
        if not bool(self._side_value(side, "enabled", True)):
            return "side_disabled_in_config"
        if self._side_entry_trigger_blocked(side):
            return "side_entry_trigger"
        if getattr(self, "_reduce_only_mode", False):
            return "reduce_only_mode"
        if self._risk_opening_pause_active():
            return str(getattr(self, "_risk_opening_pause_reason", "") or "risk_opening_pause")
        mgn_reason = self._mgn_ratio_opening_block_reason()
        if mgn_reason:
            return mgn_reason
        if self._portfolio_throttled_dominant_side_blocked(side):
            return "portfolio_throttled_dominant"
        if self._inventory_opening_margin_blocked(side):
            return "inventory_opening_margin"
        if self._trading_halted:
            return "trading_halted"
        if self._in_startup_recovery():
            return "startup_recovery"
        if self._in_post_recovery_refill_delay():
            return "post_recovery_refill_delay"
        if self._grid_pause_active(side):
            reasons = getattr(self, "_grid_pause_reasons", {})
            return str(reasons.get(side, "") or "temporary_pause")
        if self._recent_side_loss_active(side):
            return "recent_loss_guard"
        range_block = self._grid_range_side_block_reason(side)
        if range_block:
            return range_block
        # Every gate above passed yet the side is still disabled: a new gate
        # was added to _grid_side_enabled without a matching branch here.
        return "unknown_gate"

    def _grid_layer_limit_reason(self) -> str:
        reasons = []
        if self._in_startup_recovery():
            reasons.append("startup_recovery")
        if self._in_post_recovery_refill_delay():
            reasons.append("post_recovery_refill_delay")
        if self._net_imbalance_ratio() >= self._grid_bias_ratio:
            reasons.append("imbalance_clamp")
        if self._cached_equity > 0:
            margin_load = self._account_margin_load_ratio()
            if margin_load >= self._grid_expand_margin_ratio and not self._small_strategy_margin_bypass("long", self._effective_base_pos_usdt()):
                reasons.append("margin_clamp")
        free_avail = self._available_after_reserve()
        if free_avail > 0 and self._base_pos_usdt > 0:
            balance_layers = max(1, int(free_avail / (self._base_pos_usdt * 2)))
            if balance_layers < min(self._max_adds, 2):
                reasons.append("balance_cap")
        return ",".join(reasons) if reasons else "normal"

    def _log_grid_refill_block(self, side: str, reason: str):
        if not reason:
            return
        now = time.time()
        state = getattr(self, "_grid_refill_block_log_state", {})
        last_reason, last_ts = state.get(side, ("", 0.0))
        interval = 45.0 if reason == "capacity_exhausted" else max(
            30.0, safe_float(getattr(self, "_grid_refill_block_log_interval", 300.0))
        )
        if reason == last_reason and (now - last_ts) < interval:
            return
        self.log.info(f"[grid_refill] block {side}: {reason}")
        state[side] = (reason, now)
        self._grid_refill_block_log_state = state

    @staticmethod
    def _grid_refill_retry_seconds(reason: str) -> float:
        if str(reason or "") in {"max_side_notional", "max_total_notional", "max_net_notional"}:
            # Capacity is a soft, changing condition.  A short retry lets a
            # filled TP or a price move reopen the grid without leaving the
            # side paused for five minutes.  Account/margin and exchange
            # failures continue to use the longer default retry below.
            return 45.0
        return 60.0

    def _grid_capacity_qty(
        self,
        side: str,
        desired_qty: float,
        price: float,
        pending_orders=None,
        inventory_repair: bool = False,
    ) -> float:
        """Size a grid order to the capacity that remains right now.

        Grid replenishment used to submit the full target quantity and let the
        opening guard reject it when a side was nearly full.  That turns a
        partially available side into a dead side.  Reserve capacity for live
        positions and pending opens, then round the remaining quantity down to
        the exchange step.  This is only a sizing helper: the opening guard is
        still authoritative at submission time.
        """
        from strategies.hedge_risk import CapacityRiskContext, grid_capacity_qty
        ctx = CapacityRiskContext(
            side=side,
            desired_qty=desired_qty,
            price=price,
            pending_orders=pending_orders,
            inventory_repair=inventory_repair,
            long_qty=safe_float(getattr(self._long, "qty", 0.0)),
            short_qty=safe_float(getattr(self._short, "qty", 0.0)),
            ct_val=safe_float(getattr(self, "_ct_val", 0.0)),
            dynamic_notional_limits=self._dynamic_notional_limits,
            margin_first_capacity_ready=self._margin_first_capacity_ready,
            working_total_notional_ratio=safe_float(getattr(self, "_working_total_notional_ratio", 1.0)),
            max_net_notional_usdt=safe_float(getattr(self, "_max_net_notional_usdt", 0.0)),
            inventory_repair_side_cap_multiplier=safe_float(
                getattr(self, "_inventory_repair_side_cap_multiplier", 1.0)
            ),
            account_budget_snapshot=(
                getattr(self, "_account_budget_allocator", None).snapshot
                if getattr(self, "_account_budget_allocator", None) is not None
                else lambda: {}
            ),
            cur_lev=safe_float(getattr(self, "_cur_lev", 1.0)),
            margin_buffer_ratio=safe_float(getattr(self, "_margin_buffer_ratio", 0.10)),
            qty_step=safe_float(getattr(self, "_qty_step", 0.0)),
            min_qty=safe_float(getattr(self, "_min_qty", 0.0)),
            opening_order_block_reason=lambda s, q, p, pend, ir: self._opening_order_block_reason(
                s, q, p, pending_orders=pend, inventory_repair=ir
            ),
        )
        return grid_capacity_qty(ctx)

    def _grid_pause_active(self, side: str) -> bool:
        now = time.time()
        if side == "long":
            return now < getattr(self, "_long_grid_pause_until", 0.0)
        if side == "short":
            return now < getattr(self, "_short_grid_pause_until", 0.0)
        return False

    def _pause_grid_side(self, side: str, seconds: float, reason: str = "temporary_pause"):
        pause_until = time.time() + max(3.0, seconds)
        reasons = getattr(self, "_grid_pause_reasons", None)
        if not isinstance(reasons, dict):
            reasons = {"long": "", "short": ""}
            self._grid_pause_reasons = reasons
        if side == "long":
            self._long_grid_pause_until = max(getattr(self, "_long_grid_pause_until", 0.0), pause_until)
            reasons[side] = str(reason or "temporary_pause")
        elif side == "short":
            self._short_grid_pause_until = max(getattr(self, "_short_grid_pause_until", 0.0), pause_until)
            reasons[side] = str(reason or "temporary_pause")

    def _place_initial_grid(self):
        """Place one-layer refill grids around the current center price."""
        if not self._client or not self._last_price or self._trading_halted:
            return
        if self._in_startup_recovery():
            return
        now = time.time()
        price = self._last_price
        step = min(self._effective_side_step("long"), self._effective_side_step("short"))
        step_long = self._effective_side_step("long")
        step_short = self._effective_side_step("short")
        max_gl = self._target_grid_layers()
        existing = {"long": [], "short": []}
        pending = {"long": 0.0, "short": 0.0}
        try:
            for o in self._fetch_live_orders():
                if o.get("reduceOnly") == "true":
                    continue
                side = o.get("side", "")
                pside = o.get("posSide", "")
                px = float(o.get("px", 0) or 0)
                if pside in pending:
                    pending[pside] += abs(safe_float(o.get("sz", 0)))
                if px > 0 and side == "buy" and pside == "long":
                    existing["long"].append(px)
                elif px > 0 and side == "sell" and pside == "short":
                    existing["short"].append(px)
        except Exception as e:
            self.log.warning(f"[grid_refill] failed to read live grid orders: {e}")
            self._record_event("grid_skip", reason="live_order_state_unavailable", price=price)
            return
        # Preserve the exchange snapshot so stale pre-ledger orders remain visible
        # to drift replacement and duplicate-add suppression.
        self._long_grid_levels = list(existing["long"])
        self._short_grid_levels = list(existing["short"])
        long_slots = max(0, max_gl - len(existing["long"]))
        short_slots = max(0, max_gl - len(existing["short"]))
        long_block = self._grid_refill_block_reason("long")
        short_block = self._grid_refill_block_reason("short")
        self._log_grid_refill_block("long", long_block)
        self._log_grid_refill_block("short", short_block)
        if long_block and short_block:
            return
        long_rejected = False
        short_rejected = False
        base_usdt = self._effective_base_pos_usdt()
        for n in range(1, max_gl + 1):
            gpx_l = round(price * (1 - n * step_long) / 0.0001) * 0.0001
            gpx_s = round(price * (1 + n * step_short) / 0.0001) * 0.0001
            desired_l = contract_qty_for_margin(
                self._effective_base_pos_usdt("long"), gpx_l, self._cur_lev,
                self._ct_val, self._qty_step, self._min_qty,
            ) if gpx_l > 0 else 0
            desired_s = contract_qty_for_margin(
                self._effective_base_pos_usdt("short"), gpx_s, self._cur_lev,
                self._ct_val, self._qty_step, self._min_qty,
            ) if gpx_s > 0 else 0
            desired_l *= self._inventory_net_size_multiplier("long")
            desired_s *= self._inventory_net_size_multiplier("short")
            gq_l = self._grid_capacity_qty("long", desired_l, gpx_l, pending) if gpx_l > 0 else 0.0
            # Recalculate the short side after a successful long submission so
            # the second leg sees the newly reserved pending quantity.
            gq_s = 0.0
            if desired_l > gq_l + 1e-12 and gq_l > 0:
                self.log.info(
                    f"[grid_refill] resize long layer={n} desired={desired_l:.8f} qty={gq_l:.8f}"
                )
            try:
                if long_block or long_rejected:
                    pass
                elif long_slots > 0 and gq_l > 0 and not any(abs(gpx_l - ep) < gpx_l * step * 0.5 for ep in existing["long"]):
                    if self._grid_pause_active("long"):
                        continue
                    ok, edge_reason = self._has_expected_edge("long", gpx_l, gq_l, self._effective_tp_ratio(self._effective_side_tp_ratio("long", "first"), "maker"), "maker")
                    if not ok:
                        self._log_grid_refill_block("long", edge_reason or "edge_rejected")
                    if ok:
                        guard = getattr(self, "_execution_guard", None)
                        allowed, guard_reason = guard.check_order(self._symbol, "buy", "limit", gpx_l, False) if guard else (True, "ok")
                        if not allowed:
                            self._record_event("grid_skip", "long", "maker_guard", detail=guard_reason, price=gpx_l, qty=gq_l)
                            self._log_grid_refill_block("long", f"maker_guard:{guard_reason}")
                            continue
                        r = self._place_open_order(
                            "buy", gq_l, "long", "limit", px=gpx_l,
                            pending_orders=pending,
                        )
                        if self._order_succeeded(r):
                            self._long_grid_levels.append(gpx_l)
                            pending["long"] += gq_l
                            long_slots -= 1
                        else:
                            self.log.warning(f"[grid_refill] long grid reject @{gpx_l:.4f}: {r}")
                            if r.get("guard_blocked"):
                                block_reason = str(r.get("sMsg") or r.get("sCode") or "opening_guard")
                                self._set_add_block("long", block_reason)
                                long_rejected = True
                                self._pause_grid_side("long", self._grid_refill_retry_seconds(block_reason), block_reason)
                elif long_slots > 0 and not long_block and gq_l <= 0:
                    self._log_grid_refill_block("long", "capacity_exhausted")
                if short_block or short_rejected:
                    pass
                elif short_slots > 0 and not any(abs(gpx_s - ep) < gpx_s * step * 0.5 for ep in existing["short"]):
                    gq_s = self._grid_capacity_qty("short", desired_s, gpx_s, pending) if gpx_s > 0 else 0.0
                    if desired_s > gq_s + 1e-12 and gq_s > 0:
                        self.log.info(
                            f"[grid_refill] resize short layer={n} desired={desired_s:.8f} qty={gq_s:.8f}"
                        )
                    if gq_s <= 0:
                        self._log_grid_refill_block("short", "capacity_exhausted")
                        continue
                    if self._grid_pause_active("short"):
                        continue
                    if self._last_short_grid_fail_px > 0 and self._last_short_grid_fail_ts > 0:
                        same_fail_px = abs(gpx_s - self._last_short_grid_fail_px) / max(gpx_s, 0.0001) < step * 0.35
                        if same_fail_px and (now - self._last_short_grid_fail_ts) < self._short_grid_fail_cooldown:
                            continue
                    if self._last_short_grid_px > 0 and self._last_short_grid_ts > 0:
                        if abs(gpx_s - self._last_short_grid_px) / max(gpx_s, 0.0001) < step * 0.35 and (now - self._last_short_grid_ts) < 12.0:
                            continue
                    ok, edge_reason = self._has_expected_edge("short", gpx_s, gq_s, self._effective_tp_ratio(self._effective_side_tp_ratio("short", "first"), "maker"), "maker")
                    if not ok:
                        self._log_grid_refill_block("short", edge_reason or "edge_rejected")
                    if ok:
                        guard = getattr(self, "_execution_guard", None)
                        allowed, guard_reason = guard.check_order(self._symbol, "sell", "limit", gpx_s, False) if guard else (True, "ok")
                        if not allowed:
                            self._record_event("grid_skip", "short", "maker_guard", detail=guard_reason, price=gpx_s, qty=gq_s)
                            self._log_grid_refill_block("short", f"maker_guard:{guard_reason}")
                            continue
                        r = self._place_open_order(
                            "sell", gq_s, "short", "limit", px=gpx_s,
                            pending_orders=pending,
                        )
                        if self._order_succeeded(r):
                            self._short_grid_levels.append(gpx_s)
                            pending["short"] += gq_s
                            self._last_short_grid_px = gpx_s
                            self._last_short_grid_ts = now
                            self._last_short_grid_fail_px = 0.0
                            self._last_short_grid_fail_ts = 0.0
                            self._last_short_grid_fail_code = ""
                            short_slots -= 1
                        else:
                            self._last_short_grid_fail_px = gpx_s
                            self._last_short_grid_fail_ts = now
                            self._last_short_grid_fail_code = str(r.get("sCode", r.get("code", "")))
                            self.log.warning(f"[grid_refill] short grid reject @{gpx_s:.4f} code={self._last_short_grid_fail_code or '?'}")
                            if r.get("guard_blocked"):
                                block_reason = str(r.get("sMsg") or r.get("sCode") or "opening_guard")
                                self._set_add_block("short", block_reason)
                                short_rejected = True
                                self._pause_grid_side("short", self._grid_refill_retry_seconds(block_reason), block_reason)
            except Exception as e:
                if gq_s > 0:
                    self._last_short_grid_fail_px = gpx_s
                    self._last_short_grid_fail_ts = now
                    self._last_short_grid_fail_code = type(e).__name__
                self.log.warning(f"[grid_refill] refill failed at layer {n}: {e}")
        self.log.info(f"[grid_refill] layers={max_gl} center={price:.4f} long={len(self._long_grid_levels)} short={len(self._short_grid_levels)}")
        self._record_event("grid_refill", reason=f"layers_{max_gl}", detail=f"L{len(self._long_grid_levels)}/S{len(self._short_grid_levels)}", price=price)

    def _active_grid_side(self, side: str) -> bool:
        if side == "long":
            return bool(self._long_grid_levels)
        if side == "short":
            return bool(self._short_grid_levels)
        return False

    def _preserve_opening_grids_during_inventory_repair(self) -> bool:
        """Keep DRAM maker grids live while a same-band weak-side repair runs."""
        from strategies.hedge_grid import preserve_opening_grids_during_inventory_repair
        return preserve_opening_grids_during_inventory_repair(
            str(getattr(self, "_symbol", "")),
            bool(getattr(self, "_hedged_grid_allow_dominant_refill", False)),
            self._margin_first_capacity_ready,
        )

    def _should_cancel_grid_order(self, key: str, px: float, desired_levels: list, step: float, oid: str = ""):
        if px <= 0:
            return False, "bad_price"
        nearest_gap = min((abs(px - dp) / max(px, 0.0001) for dp in desired_levels), default=999.0)
        keep_band = max(step * 1.5, self._order_refresh_tolerance_pct)
        if nearest_gap < keep_band:
            return False, f"within_band_{nearest_gap:.4f}"
        cancel_key = oid or f"{key}:{round(px, 8)}"
        now = time.time()
        last = self._last_grid_cancel_ts.get(cancel_key, 0.0)
        if now - last < self._order_cancel_cooldown_sec:
            return False, "cancel_cooldown"
        self._last_grid_cancel_ts[cancel_key] = now
        return True, f"gap_{nearest_gap:.4f}"

    def _rebuild_grid(self):
        if not self._client or self._trading_halted: return None
        now = time.time()
        if now - self._last_rebuild_time < self._rebuild_cooldown:
            return None
        self._sync_balance()
        price = self._last_price
        if price <= 0: return None
        self._grid_center_price = price
        # Compute desired levels
        max_gl = self._target_grid_layers()
        desired = {"long": [], "short": []}
        step_long = self._effective_side_step("long")
        step_short = self._effective_side_step("short")
        for n in range(1, max_gl + 1):
            desired["long"].append(round(price * (1 - n * step_long) / 0.0001) * 0.0001)
            desired["short"].append(round(price * (1 + n * step_short) / 0.0001) * 0.0001)
        # Read existing non-TP orders
        existing = {"long": [], "short": []}
        try:
            live_orders = self._fetch_live_orders()
            for o in live_orders:
                if o.get("reduceOnly") == "true": continue
                side = o.get("side", ""); pside = o.get("posSide", ""); px = float(o.get("px", 0))
                if px > 0 and side == "buy" and pside == "long": existing["long"].append(px)
                elif px > 0 and side == "sell" and pside == "short": existing["short"].append(px)
        except Exception as e:
            self.log.warning(f"[余额] 查询失败: {e}")
            return None
        # Cancel orders far from desired levels
        step = min(self._effective_side_step("long"), self._effective_side_step("short"))
        try:
            for o in live_orders:
                if o.get("reduceOnly") == "true": continue
                side = o.get("side", ""); pside = o.get("posSide", ""); px = float(o.get("px", 0))
                if px <= 0: continue
                key = "long" if side == "buy" and pside == "long" else ("short" if side == "sell" and pside == "short" else "")
                if not key: continue
                should_cancel, reason = self._should_cancel_grid_order(key, px, desired[key], step, o.get("ordId", ""))
                if should_cancel:
                    self._cancel_order(o.get("ordId"))
                    self.log.info(f"[网格重建] cancel {key} grid @{px:.4f} reason={reason}")
        except Exception as e:
            self.log.warning(f"[趋势保护] 取消挂单失败: {e}")
        # Create orders for missing levels
        actions = []; self._long_grid_levels = []; self._short_grid_levels = []
        long_block = self._grid_refill_block_reason("long")
        short_block = self._grid_refill_block_reason("short")
        for n in range(max_gl):
            gpx_l = desired["long"][n]; gpx_s = desired["short"][n]
            long_usdt = self._effective_base_pos_usdt("long") * self._trend_bias_multiplier("long") if self._grid_side_enabled("long") and not long_block else 0
            short_usdt = self._effective_base_pos_usdt("short") * self._trend_bias_multiplier("short") if self._grid_side_enabled("short") and not short_block else 0
            gq_l = contract_qty_for_margin(
                long_usdt, gpx_l, self._cur_lev, self._ct_val,
                self._qty_step, self._min_qty,
            ) if long_usdt > 0 else 0
            gq_s = contract_qty_for_margin(
                short_usdt, gpx_s, self._cur_lev, self._ct_val,
                self._qty_step, self._min_qty,
            ) if short_usdt > 0 else 0
            long_existing = any(abs(gpx_l - ep) < gpx_l * step_long * 0.5 for ep in existing["long"])
            short_existing = any(abs(gpx_s - ep) < gpx_s * step_short * 0.5 for ep in existing["short"])
            if long_existing:
                self._long_grid_levels.append(gpx_l)
            elif gq_l > 0:
                ok, _ = self._has_expected_edge("long", gpx_l, gq_l, self._effective_tp_ratio(self._effective_side_tp_ratio("long", "first"), "maker"), "maker")
                if ok:
                    actions.append({"action":"buy","quantity":str(gq_l),"pos_side":"long","order_type":"limit","price":str(gpx_l)})
                    self._long_grid_levels.append(gpx_l)
            if short_existing:
                self._short_grid_levels.append(gpx_s)
            elif gq_s > 0:
                ok, _ = self._has_expected_edge("short", gpx_s, gq_s, self._effective_tp_ratio(self._effective_side_tp_ratio("short", "first"), "maker"), "maker")
                if ok:
                    actions.append({"action":"sell","quantity":str(gq_s),"pos_side":"short","order_type":"limit","price":str(gpx_s)})
                    self._short_grid_levels.append(gpx_s)
        self._last_rebuild_time = now
        self.log.info(f"[网格重建] {max_gl}层 (avail={self._cached_avail:.1f}): {gpx_l:.4f}~{price:.4f}~{gpx_s:.4f}")
        self._record_event("grid_rebuild", reason=f"layers_{max_gl}", detail=f"actions={len(actions)}", price=price)
        return {"type":"grid_rebuild","actions":actions}
