"""下单/撤单执行。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class OrderMixin:
    """下单/撤单执行（7 个方法）。"""

    def _cancel_all_live_orders(self):
        if not self._client:
            return 0
        cancelled = 0
        try:
            for order in self._fetch_live_orders():
                oid = order.get("ordId", "")
                if not oid:
                    continue
                try:
                    self._cancel_order(oid)
                    cancelled += 1
                except Exception as e:
                    self.log.warning(f"[停机保护] 撤单失败 ordId={oid}: {e}")
        except Exception as e:
            self.log.warning(f"[停机保护] 查询挂单失败: {e}")
        if cancelled:
            self.log.info(f"[停机保护] 已撤销 {cancelled} 个挂单")
        self._long_grid_levels = []
        self._short_grid_levels = []
        self._layer_tp_orders.clear()
        self._layer_tps.clear()
        self._layer_qty_map.clear()
        self._layer_side_map.clear()
        self._active_tp_orders = 0
        return cancelled

    def _cancel_opening_orders(self):
        if not self._client:
            return 0
        try:
            orders = self._fetch_live_orders()
        except Exception as exc:
            self.log.warning(f"[reduce_only] live order query failed: {exc}")
            return 0
        cancelled = 0
        for order in orders:
            if str(order.get("reduceOnly", "")).lower() == "true":
                continue
            oid = str(order.get("ordId") or "")
            if oid and self._cancel_order(oid):
                cancelled += 1
        if cancelled:
            self.log.warning(f"[reduce_only] cancelled {cancelled} opening orders for {self._symbol}")
        self._long_grid_levels = []
        self._short_grid_levels = []
        return cancelled

    def _submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        ord_type: str = "market",
        px=None,
        pos_side: str = "",
        reduce_only: bool = False,
        tgt_ccy: str = "",
        pending_orders=None,
        paired_net_neutral: bool = False,
        inventory_repair: bool = False,
        allow_hard_total_cap: bool = False,
    ):
        from strategies.hedge_execution import submit_order
        return submit_order(
            self,
            symbol,
            side,
            qty,
            ord_type=ord_type,
            px=px,
            pos_side=pos_side,
            reduce_only=reduce_only,
            tgt_ccy=tgt_ccy,
            pending_orders=pending_orders,
            paired_net_neutral=paired_net_neutral,
            inventory_repair=inventory_repair,
            allow_hard_total_cap=allow_hard_total_cap,
        )

    def _cancel_order(self, ord_id: str, inst_id: str = "") -> bool:
        from strategies.hedge_execution import cancel_order
        return cancel_order(self, ord_id, inst_id)

    def _place_open_order(
        self,
        side: str,
        qty: float,
        pos_side: str,
        ord_type: str = "market",
        px=None,
        pending_orders=None,
        paired_net_neutral: bool = False,
        inventory_repair: bool = False,
        allow_hard_total_cap: bool = False,
    ):
        from strategies.hedge_execution import place_open_order
        return place_open_order(
            self,
            side,
            qty,
            pos_side,
            ord_type=ord_type,
            px=px,
            pending_orders=pending_orders,
            paired_net_neutral=paired_net_neutral,
            inventory_repair=inventory_repair,
            allow_hard_total_cap=allow_hard_total_cap,
        )

    def _do_add(
        self,
        side: str,
        price: float,
        max_qty: float = 0.0,
        paired_net_neutral: bool = False,
        reason_code: str = "",
        allow_hard_total_cap: bool = False,
    ) -> dict:
        pos = self._long if side == "long" else self._short
        bias_mult = self._trend_bias_multiplier(side)
        inventory_mult = self._inventory_net_size_multiplier(side)
        if inventory_mult <= 0:
            self._set_add_block(side, "inventory_net_hard_limit")
            self._record_event("add_skip", side, "inventory_net_hard_limit", price=price)
            return {"type": "skip", "reason": "inventory_net_hard_limit"}
        bias_mult *= inventory_mult
        if bias_mult <= 0:
            if reason_code in {"missing_side_repair", "inventory_repair"}:
                # Explicit weak-side repairs reduce net risk; passive-grid
                # pauses and trend bias must not prevent that safety action.
                bias_mult = max(
                    0.2,
                    self._skew_side_multiplier(side) * self._inventory_age_multiplier(side),
                )
            else:
                self._set_add_block(side, "trend_bias_block")
                self._record_event("add_skip", side, "trend_bias_block", price=price)
                return {"type":"skip","reason":"trend_bias_block"}
        side_base = self._effective_base_pos_usdt(side)
        side_mult = self._effective_side_multiplier(side)
        add_usdt = side_base * (1 + (side_mult - 1) * pos.adds) * bias_mult
        fixed_qty = 0.0
        if max_qty > 0:
            raw_qty = contract_qty_for_margin(
                add_usdt, price, self._cur_lev, self._ct_val, self._qty_step
            )
            capped_qty = math.floor((max_qty + 1e-12) / self._qty_step) * self._qty_step
            # A paired leg must match the already submitted weak leg exactly;
            # normal side sizing can turn a neutral pair into a net-increasing
            # order that RiskGate correctly rejects.
            if reason_code in {"paired_weak", "paired_dominant"}:
                fixed_qty = capped_qty
            else:
                fixed_qty = min(max(raw_qty, self._min_qty), capped_qty)
            if fixed_qty + 1e-12 < self._min_qty:
                self._set_add_block(side, f"{reason_code or 'add'}_below_min_qty")
                return {"type": "skip", "reason": f"{reason_code or 'add'}_below_min_qty"}
            add_usdt = fixed_qty * price * self._ct_val / max(self._cur_lev, 1)
        # 单策略仓位硬上限：用本策略自身成本/保证金口径，不把其它币种占用算进来。
        # 全账户保证金风险仍由 margin_load_* / margin_clamp 负责限流。
        if self._cached_equity > 0:
            max_margin = self._cached_equity * self._margin_cap_ratio_for_side(side)
            total_margin = self._strategy_margin_used() + add_usdt
            if total_margin > max_margin:
                self._set_add_block(side, "position_limit")
                self._record_event("add_skip", side, "position_limit", detail=f"strategy {total_margin:.1f}>{max_margin:.1f}", price=price)
                return {"type":"skip","reason":f"仓位超限 strategy_margin={total_margin:.1f}>{max_margin:.1f}"}
        valid, reason = self._validate_order(side, add_usdt, reason_code=reason_code)
        if not valid:
            self._set_add_block(side, reason)
            self._record_event("add_skip", side, reason, price=price)
            return {"type":"skip","reason":reason}
        if self._price_dedup and pos.entry > 0 and reason_code not in {"inventory_repair", "paired_weak", "paired_dominant"}:
            if abs(pos.entry - price) / price < self._effective_side_step(side) * 0.3:
                self._set_add_block(side, "price_dedup")
                self._record_event("add_skip", side, "price_dedup", price=price)
                return {"type":"skip","reason":"price_dedup"}
        if fixed_qty > 0:
            qty = fixed_qty
        else:
            size_ratio = safe_float(getattr(self, "_floating_loss_ratio", 1.0))
            if bool(getattr(self, "_floating_loss_freeze_active", False)) and self._is_weak_side(side):
                # Weak-side repair is risk-reducing; do not apply the dominant
                # side's floating-loss shrink to it.
                size_ratio = 1.0
            add_usdt = add_usdt * size_ratio
            qty = contract_qty_for_margin(
                add_usdt, price, self._cur_lev, self._ct_val,
                self._qty_step, self._min_qty,
            )
        tp_mult = 1.0 if reason_code in {
            "paired_weak",
            "paired_dominant",
            "inventory_repair",
        } else (1 + (side_mult - 1) * pos.adds)
        tp_r = self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * tp_mult, "maker")
        edge_ok, edge_reason = self._has_expected_edge(side, price, qty, tp_r, "taker")
        if not edge_ok and reason_code == "inventory_repair" and bool(getattr(self, "_inventory_repair_relax_edge_enabled", False)):
            # Inventory repair may use break-even economics, but never a
            # materially losing order.  Account margin, TP coverage, net cap,
            # and exchange guards still run independently below.
            notional = max(price * qty * self._ct_val, 0.0)
            fees = notional * (self.TAKER_FEE + self.MAKER_FEE)
            slippage = notional * 0.0003
            modeled_net = notional * tp_r - fees - slippage
            repair_floor = -max(0.0, safe_float(getattr(self, "_coverage_safety_tp_max_loss_usdt", 0.05)))
            if modeled_net >= repair_floor:
                edge_ok, edge_reason = True, ""
        if not edge_ok:
            self._set_add_block(side, edge_reason)
            self._record_event("add_skip", side, edge_reason, price=price, qty=qty)
            return {"type":"skip","reason":edge_reason}
        # FIX: place market order BEFORE updating local state
        if self._client:
            order = "buy" if side == "long" else "sell"
            result = self._place_open_order(
                order,
                qty,
                side,
                paired_net_neutral=paired_net_neutral,
                inventory_repair=(reason_code in {"inventory_repair", "missing_side_repair"}),
                allow_hard_total_cap=allow_hard_total_cap,
            )
            if not self._order_succeeded(result):
                code = self._order_error_code(result) or "unknown"
                message = ""
                if isinstance(result, dict):
                    message = str(result.get("sMsg") or result.get("msg") or "")
                if isinstance(result, dict) and result.get("guard_blocked"):
                    reason = message or code or "opening_guard"
                    self.log.info(
                        "[加仓] %s opening blocked symbol=%s code=%s msg=%s qty=%.8f px=%.8f",
                        side, self._symbol, code, reason, qty, price,
                    )
                    self._set_add_block(side, reason)
                    self._record_event("add_skip", side, reason, price=price, qty=qty)
                    return {"type": "skip", "reason": reason}
                self.log.warning(
                    "[加仓] %s market order rejected symbol=%s code=%s msg=%s qty=%.8f px=%.8f",
                    side, self._symbol, code, message, qty, price,
                )
                self._set_add_block(side, "market_order_failed")
                self._record_event(
                    "add_skip", side, "market_order_failed",
                    detail=f"code={code} msg={message}", price=price, qty=qty,
                )
                return {"type":"skip","reason":"market_order_failed_"+side,"code":code,"message":message}
            opening_order_id = self._extract_order_id(result)
            fee = qty * price * self._ct_val * self.TAKER_FEE
            self._total_fees += fee
        else:
            fee = 0.0
        new_cost = pos.cost + add_usdt; new_qty = pos.qty + qty
        pos.entry = (pos.qty * pos.entry + qty * price) / new_qty if new_qty > 0 else 0
        pos.qty = new_qty; pos.cost = new_cost; pos.adds += 1
        if side == 'long':
            self._long_layer += 1
        else:
            self._short_layer += 1
        self._add_to_segment(side, price, qty, add_usdt)
        self._last_add = time.time()
        if side == "long":
            self._last_add_long = time.time()
        else:
            self._last_add_short = time.time()
        self._order_records.append(
            (time.time(), side, add_usdt, self._add_rate_limit_bucket(reason_code))
        )
        self._last_add_candle = self._current_candle_id
        self._set_add_block(side, "")
        lid = str(self._next_layer_id); self._next_layer_id += 1
        self._layer_qty_map[lid] = qty  # track exact qty for layered TP
        self._layer_side_map[lid] = side
        if side == "long": self._layer_tps[lid] = price * (1 + tp_r)
        else: self._layer_tps[lid] = price * (1 - tp_r)
        self._place_tp_order(lid, side, qty, self._layer_tps[lid])
        order = "buy" if side == "long" else "sell"
        self.log.info(f"[加仓] {side}#{pos.adds}: {price:.2f} qty={qty:.4f} fee={fee:.4f}U")
        self._record_event("add", side, reason_code or f"{side}_layer_{pos.adds}", detail=f"tp={self._layer_tps[lid]:.4f}", price=price, qty=qty)
        self._save_pnl_data()
        return {
            "type":"add",
            "side":side,
            "action":order,
            "quantity":str(qty),
            "pos_side":side,
            "cost":str(add_usdt),
            "order_id":opening_order_id,
        }

    # ==================== Opbot辅助方法 ====================

    def _close_side(self, side: str) -> dict:
        """Close one side only after exchange submission or no-position confirmation."""
        pos = self._long if side == "long" else self._short
        if pos.qty <= 0:
            return {"type":"skip","reason":side+"_no_position"}
        price = self._last_price
        close_qty = round(pos.qty / self._qty_step) * self._qty_step
        if close_qty <= 0:
            return {"type":"skip","reason":side+"_invalid_qty"}
        contract_value = max(self._ct_val, 1e-12)
        gross = contract_value * ((price - pos.entry) * close_qty if side == "long" else (pos.entry - price) * close_qty)
        fee = close_qty * contract_value * price * self.TAKER_FEE if self._client else 0.0
        net = gross - fee if self._client else 0.0
        order = "sell" if side == "long" else "buy"
        self.log.info(f"[CloseSide] {side}: qty={close_qty:.4f}@{pos.entry:.4f} gross={gross:.2f}U net={net:.2f}U")
        clear_local = not self._client
        if self._client:
            try:
                result = self._submit_order(
                    self._symbol, order, str(close_qty),
                    ord_type="market", pos_side=side, reduce_only=True
                )
            except Exception as e:
                self.log.error("[CloseSide] %s close failed: %s" % (side, e))
                return {"type":"close_failed","side":side,"reason":"exception","error":str(e)}
            if self._order_succeeded(result):
                self.log.info("[CloseSide] %s market close %s ordId=%s net=%.2fU" % (side, close_qty, self._extract_order_id(result), net))
                refresh = self._refresh_position_after_reduce(side)
                if not refresh.get("confirmed"):
                    self.log.warning(
                        "[CloseSide] %s accepted but remaining position is unconfirmed: %s",
                        side,
                        refresh,
                    )
                    return {
                        "type": "close_pending",
                        "side": side,
                        "reason": refresh.get("reason", "position_state_unavailable"),
                        "order_id": self._extract_order_id(result),
                    }
                clear_local = bool(refresh.get("flat"))
                remaining_qty = safe_float(refresh.get("qty", 0.0))
                if not clear_local:
                    for lid in list(self._layer_tps.keys()):
                        if self._tp_lid_side(lid) == side:
                            self._cancel_tp_order(lid)
                            self._layer_tps.pop(lid, None)
                            self._layer_qty_map.pop(lid, None)
                            self._layer_side_map.pop(lid, None)
                    self._tp_init_done = False
                    self._init_existing_tp(cancel_existing=False)
                    self._save_pnl_data()
                    return {
                        "type": "close_side_partial",
                        "side": side,
                        "action": order,
                        "quantity": str(close_qty),
                        "remaining_qty": remaining_qty,
                        "pos_side": side,
                        "reduce_only": True,
                        "pnl": round(net, 4),
                    }
            elif self._order_error_code(result) == "51169":
                refresh = self._refresh_position_after_reduce(side)
                if refresh.get("confirmed") and not refresh.get("flat"):
                    self._tp_init_done = False
                    self._init_existing_tp(cancel_existing=False)
                if not refresh.get("confirmed") or not refresh.get("flat"):
                    self.log.warning("[CloseSide] %s close rejected and position is not confirmed flat: %s", side, refresh)
                    return {"type":"close_failed","side":side,"reason":"close_unconfirmed","result":result}
                fee = 0.0
                net = 0.0
                self.log.info("[CloseSide] %s exchange confirms flat; clearing local state" % side)
                clear_local = True
            else:
                self.log.warning("[CloseSide] %s close rejected: %s" % (side, result))
                return {"type":"close_failed","side":side,"reason":"order_rejected","result":result}

        if not clear_local:
            return {"type":"skip","side":side,"reason":"close_not_confirmed"}
        for lid in list(self._layer_tps.keys()):
            if self._tp_lid_side(lid) == side:
                self._cancel_tp_order(lid)
                self._layer_tps.pop(lid, None)
                self._layer_qty_map.pop(lid, None)
                self._layer_side_map.pop(lid, None)
        if side == "long":
            self._long = Pos("long"); self._long_layer = 0
            self._clear_segments("long")
        else:
            self._short = Pos("short"); self._short_layer = 0
            self._clear_segments("short")
        self._save_pnl_data()
        return {"type":"close_side","side":side,"action":order,"quantity":str(close_qty),
                "pos_side":side,"reduce_only":True,"pnl":round(net,4)}
