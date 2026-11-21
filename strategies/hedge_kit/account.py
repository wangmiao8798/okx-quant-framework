"""账户同步、余额与杠杆。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class AccountMixin:
    """账户同步、余额与杠杆（29 个方法）。"""

    def _sync_recent_fills(self):
        if not self._client:
            return
        now = time.time()
        if self._last_fill_sync > 0 and now - self._last_fill_sync < self._fill_sync_interval:
            return
        self._last_fill_sync = now
        try:
            anchor_ts_ms = int(self._fill_sync_ts_ms)
            if anchor_ts_ms <= 0 and self._last_pnl_save_ts_ms > 0:
                anchor_ts_ms = self._last_pnl_save_ts_ms + 3000
            resp = self._client._call(
                self._client._trade.get_fills,
                instType="SWAP",
                instId=self._symbol,
                limit="100",
            )
            fills = resp.get("data", []) or []
        except Exception as e:
            self.log.warning(f"[PnLSync] fill sync failed: {e}")
            return
        if not fills:
            self._maybe_flush_pnl_heartbeat()
            return
        seen = set(self._recent_fill_ids)
        changed = False
        processed = 0
        latest_ts_ms = int(self._fill_sync_ts_ms)
        ordered = sorted(
            fills,
            key=lambda item: (
                int(safe_float(item.get("ts", 0))),
                str(item.get("tradeId") or item.get("billId") or item.get("ordId") or ""),
            ),
        )
        for fill in ordered:
            ts_ms = int(safe_float(fill.get("ts", 0)))
            if anchor_ts_ms > 0 and ts_ms > 0 and ts_ms <= anchor_ts_ms:
                continue
            fill_id = str(
                fill.get("tradeId")
                or fill.get("billId")
                or f"{fill.get('ordId', '')}:{fill.get('side', '')}:{fill.get('posSide', '')}:{fill.get('fillPx', '')}:{fill.get('fillSz', '')}:{fill.get('ts', '')}"
            )
            if fill_id in seen:
                continue
            fee = abs(safe_float(fill.get("fee", 0)))
            fill_pnl = safe_float(fill.get("fillPnl", 0))
            side = fill.get("side", "")
            pos_side = fill.get("posSide", "")
            is_reduce = self._is_reduce_fill(side, pos_side, fill_pnl)
            self._record_fill_to_database(
                fill, fill_id, side, pos_side, fee, fill_pnl, is_reduce
            )
            if pos_side in {"long", "short"}:
                if not hasattr(self, "_normal_close_sides"):
                    self._normal_close_sides = {"long": False, "short": False}
                if is_reduce:
                    self._normal_close_sides[pos_side] = True
                else:
                    # A new opening fill starts a fresh hedge lifecycle.
                    self._normal_close_sides[pos_side] = False
            if fee > 0:
                self._total_fees += fee
                changed = True
            if is_reduce:
                net = fill_pnl - fee
                self._total_pnl += net
                self._record_fill_close_transaction(fill, fee, fill_pnl)
                self._mark_side_realized_pnl(pos_side, net)
                changed = True
            seen.add(fill_id)
            self._recent_fill_ids.append(fill_id)
            self._recent_fill_ids = self._recent_fill_ids[-200:]
            latest_ts_ms = max(latest_ts_ms, ts_ms)
            processed += 1
            self.log.info(
                f"[PnLSync] tradeId={fill.get('tradeId', '')} side={side}/{pos_side} fee={fee:.4f} pnl={fill_pnl:.4f}"
            )
        if latest_ts_ms > 0:
            self._fill_sync_ts_ms = latest_ts_ms
        if processed > 0 or changed:
            self._save_pnl_data()
            self.log.info(f"[PnLSync] synced fills={processed} realized={self._total_pnl:.4f} fees={self._total_fees:.4f}")
        else:
            self._maybe_flush_pnl_heartbeat()

    def _net_imbalance_ratio(self) -> float:
        from strategies.hedge_calculations import net_imbalance_ratio
        return net_imbalance_ratio(self._inventory_or_create)

    def _small_strategy_margin_bypass(self, side: str, add_usdt: float = 0.0) -> bool:
        # Let very small profiles keep quoting even when another symbol consumes most
        # account margin. This only bypasses soft margin_load gates; hard per-strategy
        # position/margin checks and exchange available-margin checks still apply.
        if self._cached_equity <= 0:
            return False
        account_margin_load = self._account_margin_load_ratio()
        if account_margin_load >= 0.70:
            return False
        strategy_ratio = (self._strategy_margin_used() + max(0.0, add_usdt)) / self._cached_equity
        if strategy_ratio > 0.02:
            return False
        if add_usdt > max(2.0, self._cached_equity * 0.002):
            return False
        return True

    def _margin_first_capacity_ready(self) -> bool:
        """Return whether the FART profile may use account-margin-first capacity."""
        if not bool(getattr(self, "_margin_first_capacity_enabled", False)):
            return False
        if self._account_margin_data_block_reason():
            return False
        if self._mgn_ratio_opening_block_reason():
            return False
        return self._account_margin_load_ratio() < safe_float(
            getattr(self, "_margin_first_max_margin_ratio", 0.50)
        )

    @property
    def _account_state_or_create(self):
        state = getattr(self, "_account_state", None)
        if state is None:
            state = AccountRiskState(getattr(self, "_client", None), getattr(self, "log", None))
            self._account_state = state
        return state

    @property
    def _account_risk_snapshot(self):
        return self._account_state_or_create.snapshot

    @_account_risk_snapshot.setter
    def _account_risk_snapshot(self, value):
        self._account_state_or_create.snapshot = value if isinstance(value, dict) else {}

    def _sync_balance(self) -> bool:
        state = self._account_state_or_create
        result = state.sync()
        allocator = getattr(self, "_account_budget_allocator", None)
        if allocator is not None:
            allocator.update(
                self._cached_equity,
                self._cached_avail,
                self._cached_frozen,
                self._account_mgn_ratio(),
            )
        total_eq = safe_float((self._account_risk_snapshot.get("account") or {}).get("totalEq"))
        if result and total_eq > 0 and self._risk_equity_baseline > 0:
            self._handle_stale_baseline(total_eq)
        if result and self._risk_equity_baseline <= 0 and total_eq > 0:
            self._risk_equity_baseline = total_eq
            self._initial_equity = total_eq
            self._save_pnl_data()
            self.log.info(f"[浣欓] 鍒濆鏉冪泭: {total_eq:.2f}U")
        return result

    def _account_mgn_ratio(self) -> float:
        raw = self._account_risk_snapshot if isinstance(self._account_risk_snapshot, dict) else {}
        usdt = raw.get("usdt") if isinstance(raw.get("usdt"), dict) else {}
        return max(0.0, safe_float(usdt.get("mgnRatio")))

    def _account_margin_data_block_reason(self) -> str:
        """Require fresh account margin data without treating hedge liqPx as mandatory."""
        if not self._client:
            return ""
        raw = self._account_risk_snapshot if isinstance(self._account_risk_snapshot, dict) else {}
        observed_ms = safe_float(raw.get("observed_at_ms"))
        if observed_ms <= 0:
            return "account_margin_data_unavailable"
        age = max(0.0, time.time() - observed_ms / 1000.0)
        if age > self._account_risk_max_age_sec:
            return f"account_margin_data_stale:{age:.1f}s"
        minimum_equity = max(0.0, safe_float(getattr(self, "_min_open_equity_usdt", 0.0)))
        if minimum_equity > 0.0:
            equity = safe_float(getattr(self, "_cached_equity", 0.0))
            if equity <= 0.0:
                return "account_equity_unavailable"
            if equity < minimum_equity:
                return "account_equity_below_minimum"
        # An empty mgnRatio is normal on an otherwise flat account. Once this
        # strategy has inventory, a missing ratio must fail closed.
        if self._long.qty > 0 or self._short.qty > 0:
            if self._account_mgn_ratio() <= 0:
                return "account_margin_ratio_unavailable"
        return ""

    def _account_budget_snapshot(self) -> dict:
        allocator = getattr(self, "_account_budget_allocator", None)
        if allocator is None:
            return {"enabled": False, "remaining_margin": 0.0}
        return allocator.snapshot()

    def _order_margin_required(self, qty: float, price: float) -> float:
        from strategies.hedge_risk import order_margin_required
        return order_margin_required(
            qty,
            price,
            safe_float(getattr(self, "_ct_val", 1.0)),
            safe_float(getattr(self, "_cur_lev", 1.0)),
            safe_float(getattr(self, "_margin_buffer_ratio", 0.10)),
        )

    def _account_budget_block_reason(self, qty: float, price: float, pending_margin: float = 0.0) -> str:
        from strategies.hedge_risk import account_budget_block_reason
        allocator = getattr(self, "_account_budget_allocator", None)
        remaining = None if allocator is None else allocator.remaining_margin()
        return account_budget_block_reason(
            remaining,
            self._order_margin_required(qty, price),
            pending_margin,
        )

    def _pending_opening_margin(self, orders=None) -> float:
        if orders is None:
            try:
                orders = self._fetch_live_orders()
            except Exception:
                return 0.0
        total = 0.0
        for order in orders or []:
            if str(order.get("reduceOnly", "false")).lower() == "true":
                continue
            order_px = safe_float(order.get("px")) or safe_float(getattr(self, "_last_price", 0.0))
            total += self._order_margin_required(safe_float(order.get("sz")), order_px)
        return total

    def _set_leverage(self, symbol: str, lever: int, margin_mode: str = ""):
        from strategies.hedge_execution import set_leverage
        return set_leverage(self, symbol, lever, margin_mode)

    def _sync_pending_orders(self, allow_grid_rebuild=True):
        """从交易所同步挂单到引擎内部状态"""
        if not self._client or self._trading_halted:
            return
        try:
            orders = self._fetch_live_orders()
            self._update_order_tracker(orders)
            # Only skip grid placement if there are non-TP orders
            grid_orders = [o for o in orders if o.get("reduceOnly") != "true"]
            # Exchange opening orders are the source of truth. Clear stale
            # in-memory levels before deciding whether a refill is required;
            # otherwise a cancelled/expired level can falsely trigger
            # passive_grid_active forever.
            self._long_grid_levels = []
            self._short_grid_levels = []
            for o in grid_orders:
                side = o.get("side", "")
                pside = o.get("posSide", "")
                px = float(o.get("px", 0) or 0)
                if px <= 0:
                    continue
                if side == "buy" and pside == "long":
                    self._long_grid_levels.append(px)
                elif side == "sell" and pside == "short":
                    self._short_grid_levels.append(px)
            if not grid_orders:
                if allow_grid_rebuild:
                    self._place_initial_grid()
                else:
                    self.log.info("[订单同步] startup recovery: no grid orders, skip rebuild")
                return
            restored_tp = 0; restored_grid = 0
            for o in orders:
                side = o.get("side", "")
                pside = o.get("posSide", "")
                px = float(o.get("px", 0))
                reduce = o.get("reduceOnly") == "true"
                if reduce and px > 0:
                    target_idx = 0 if pside == "long" else 1
                    lid = self._next_layer_id
                    while lid % 2 != target_idx:
                        lid += 1
                    self._layer_tps[str(lid)] = px
                    self._next_layer_id = lid + 1
                    restored_tp += 1
                    self.log.info(f"[挂单同步] TP {pside} @ {px:.4f}")
                elif px > 0:
                    if side == "buy" and pside == "long":
                        if px not in self._long_grid_levels:
                            self._long_grid_levels.append(px)
                            restored_grid += 1
                            self.log.info(f"[挂单同步] 网格多单 @ {px:.4f}")
                    elif side == "sell" and pside == "short":
                        if px not in self._short_grid_levels:
                            self._short_grid_levels.append(px)
                            restored_grid += 1
                            self.log.info(f"[挂单同步] 网格空单 @ {px:.4f}")
            self._active_tp_orders = restored_tp
        except Exception as e:
            self.log.warning(f"[挂单同步] 失败: {e}")

    def _sync_exchange_state(self):
        try:
            rows = self._fetch_position_rows()
            has_long = False; has_short = False
            for p in rows:
                self._observe_exchange_position_risk(p)
                qty = safe_float(p.get("pos", 0))
                if abs(qty) > self._position_qty_tolerance():
                    side = p.get("posSide"); entry = float(p.get("avgPx", 0))
                    imr = safe_float(p.get("imr"))
                    actual_lev = max(1, int(round(safe_float(p.get("lever")))) or self._cur_lev)
                    cost = imr if imr > 0 else abs(qty * entry * max(self._ct_val, 1e-12) / actual_lev)
                    if side == "long":
                        self._long = Pos("long", entry, qty, cost, 0)
                        self._update_position_age("long", qty)
                        self._long.adds = self._estimate_adds(qty, entry)
                        has_long = True
                    elif side == "short":
                        self._short = Pos("short", entry, qty, cost, 0)
                        self._update_position_age("short", qty)
                        self._short.adds = self._estimate_adds(qty, entry)
                        has_short = True
                    self._add_to_segment(side, entry, qty, cost)
            self._init_done = bool(has_long or has_short)
            if self._init_done:
                self._grid_center_price = self._last_price
            self.log.info(f"[sync] long={self._long.qty} short={self._short.qty}")
            if has_long or has_short:
                if self._trading_halted or self._is_hard_stop_active():
                    self._engage_halt("startup_hard_stop")
                    self._cancel_all_live_orders()
                    return True
                startup_reason = "two_sided" if has_long and has_short else ("long_only" if has_long else "short_only")
                self._enter_startup_recovery(startup_reason)
                self._record_event("sync", reason=startup_reason, detail=f"L{self._long.qty:.0f}/S{self._short.qty:.0f}", price=self._last_price)
                if has_long and not has_short:
                    normal_close = bool(getattr(self, "_normal_close_sides", {}).get("short", False))
                    if self._startup_auto_hedge_missing_side and not normal_close:
                        base_pos_usdt = self._effective_base_pos_usdt()
                        q = contract_qty_for_margin(
                            base_pos_usdt, self._last_price, self._base_lev,
                            self._ct_val, self._qty_step, self._min_qty,
                        )
                        self._short = Pos("short", self._last_price, q, base_pos_usdt, 0)
                        self.log.info(f"[sync] missing short open qty={q}")
                        if self._client:
                            try: self._place_open_order("sell", q, "short")
                            except Exception as e:
                                self.log.warning(f"[sync] 缺失空单补开失败: {e}")
                    else:
                        reason = "normal_tp_exit" if normal_close else "auto_hedge_disabled"
                        self.log.info(f"[sync] startup long-only position detected, skip auto short hedge reason={reason}")
                elif has_short and not has_long:
                    normal_close = bool(getattr(self, "_normal_close_sides", {}).get("long", False))
                    if self._startup_auto_hedge_missing_side and not normal_close:
                        base_pos_usdt = self._effective_base_pos_usdt()
                        q = contract_qty_for_margin(
                            base_pos_usdt, self._last_price, self._base_lev,
                            self._ct_val, self._qty_step, self._min_qty,
                        )
                        self._long = Pos("long", self._last_price, q, base_pos_usdt, 0)
                        self.log.info(f"[sync] missing long open qty={q}")
                        if self._client:
                            try: self._place_open_order("buy", q, "long")
                            except Exception as e:
                                self.log.warning(f"[sync] 缺失多单补开失败: {e}")
                    else:
                        reason = "normal_tp_exit" if normal_close else "auto_hedge_disabled"
                        self.log.info(f"[sync] startup short-only position detected, skip auto long hedge reason={reason}")
                # 恢复流程频率门控：挂单同步与 TP 接管至少间隔 60 秒执行一次，
                # 避免每次成功 sync 都重复高频调用 OKX API 触发限流(50011)降级，
                # 同时保留定期恢复能力（新开仓后仍能补挂/接管 TP）。
                _now = time.time()
                if _now - float(getattr(self, "_last_startup_recovery_ts", 0.0)) >= 60.0:
                    self._sync_pending_orders(allow_grid_rebuild=self._startup_rebuild_grids)
                    self._init_existing_tp(
                        cancel_existing=bool(getattr(self, "_refresh_tp_on_startup", False))
                    )
                    self._last_startup_recovery_ts = _now
                return True
            # 空查询结果防护：API 降级或本地仍有跟踪挂单时，查询结果不可信
            # （OKXClient.api_degraded 注释：降级窗口内可能返回 code=0 data=[]），
            # 保留本地仓位状态，避免误判空仓清空仓位后造成失管。
            degraded = bool(self._client and self._client.api_degraded())
            tracker_total = 0
            tracker = getattr(self, "_order_tracker", None)
            if tracker is not None:
                try:
                    tracker_total = int(tracker.summary().get("total", 0) or 0)
                except Exception:
                    tracker_total = 0
            if tracker_total > 0 or degraded:
                self.log.warning(
                    f"[sync] empty position rows live_orders={tracker_total} degraded={degraded}; keep local state"
                )
                return False
            self.log.info("[sync] exchange flat; awaiting initial hedge")
        except Exception as e:
            self.log.warning(f"[同步持仓] 读取账户持仓失败: {e}")
        return False
    # ==================== 保证金(动态) ====================

    def _margin_cap_ratio_for_side(self, side: str) -> float:
        base = max(0.1, min(self._max_margin_ratio, 0.8))
        if self._is_weak_side(side):
            return max(base, max(0.1, min(self._weak_side_margin_ratio, 1.05)))
        return base

    def _account_margin_load_ratio(self) -> float:
        if self._cached_equity <= 0:
            return 0.0
        return max(0.0, self._cached_frozen / self._cached_equity)

    def _strategy_margin_used(self) -> float:
        # Per-strategy margin/cost budget. Do not use account-level frozen margin here,
        # otherwise a large position in one symbol incorrectly blocks small independent
        # profiles such as CRV/FARTCOIN. Account-wide risk remains controlled by
        # _account_margin_load_ratio() / margin_load_* gates.
        imr_by_side = getattr(self, "_exchange_position_imr_by_side", {})
        imr_total = sum(safe_float(value) for value in imr_by_side.values()) if isinstance(imr_by_side, dict) else 0.0
        imr_age = time.time() - safe_float(getattr(self, "_last_exchange_position_risk_ts", 0.0))
        if imr_total > 0 and imr_age <= 15.0:
            return imr_total
        return max(0.0, self._long.cost + self._short.cost)

    def _check_margin(self, side, add_usdt=None):
        if add_usdt is None: add_usdt = self._effective_base_pos_usdt(side)
        eq = self._cached_equity if self._cached_equity > 0 else 80.0
        max_total = eq * self._margin_cap_ratio_for_side(side)
        total = self._strategy_margin_used() + add_usdt
        return total <= max_total

    def _fetch_position_rows(self):
        self._last_position_query_ok = False
        response = self._client._call(self._client._account.get_positions, instId=self._symbol)
        if not response or str(response.get("code", "0")) != "0":
            raise RuntimeError(f"position query failed: {response}")
        rows = response.get("data")
        if not isinstance(rows, list):
            raise RuntimeError(f"invalid position query payload: {response}")
        self._last_position_query_ok = True
        return rows

    def _fetch_exchange_positions(self):
        result = {"long": Pos("long"), "short": Pos("short")}
        for row in self._fetch_position_rows():
            side = str(row.get("posSide", "")).lower()
            if side not in result:
                continue
            qty = abs(safe_float(row.get("pos", 0)))
            entry = safe_float(row.get("avgPx", 0))
            if qty > 0 and entry > 0:
                result[side] = Pos(side, entry=entry, qty=qty)
        return result

    def _fetch_live_orders(self):
        response = self._client._call(
            self._client._trade.get_order_list, instId=self._symbol, state="live"
        )
        if not response or str(response.get("code", "0")) != "0":
            raise RuntimeError(f"order query failed: {response}")
        orders = response.get("data", [])
        if not isinstance(orders, list):
            raise RuntimeError(f"invalid order query payload: {response}")
        self._update_exchange_tp_coverage(orders)
        return orders

    def _log_balance(self):
        self._account_state_or_create.log_balance(self._total_pnl, self._total_fees)

    def _has_leverage_mismatch(self) -> bool:
        configured = int(round(safe_float(getattr(self, "_base_lev", 0))))
        if configured <= 0:
            return False
        return any(
            safe_float(value) > 0 and int(round(safe_float(value))) != configured
            for value in getattr(self, "_exchange_leverage_by_side", {}).values()
        )

    def _sync_positions(self):
        """每个tick从交易所同步持仓，检测多空变化"""
        if not self._client: return
        try:
            rows = self._fetch_position_rows()
            found_long = False; found_short = False
            position_grew = False
            position_changed = False
            for p in rows:
                self._observe_exchange_position_risk(p)
                qty = safe_float(p.get("pos", 0))
                if abs(qty) < self._position_qty_tolerance(): continue
                side = p.get("posSide"); entry = float(p.get("avgPx", 0))
                imr = safe_float(p.get("imr"))
                actual_lev = max(1, int(round(safe_float(p.get("lever")))) or self._cur_lev)
                position_cost = imr if imr > 0 else abs(qty * entry * max(self._ct_val, 1e-12) / actual_lev)
                if side == "long":
                    found_long = True
                    self._tp_no_position_until["long"] = 0.0
                    self._position_missing_counts["long"] = 0
                    self._long.cost = position_cost
                    if abs(qty - self._long.qty) > self._position_qty_tolerance():
                        position_changed = True
                        diff = qty - self._long.qty
                        self._long.qty = qty; self._long.entry = entry
                        self._update_position_age("long", qty)
                        if diff > 0:
                            self._long.adds += 1
                        elif diff < -self._position_qty_tolerance():
                            if not hasattr(self, "_normal_close_sides"):
                                self._normal_close_sides = {"long": False, "short": False}
                            self._normal_close_sides["long"] = True
                        self._long_layer = self._estimate_adds(qty, entry)
                        self.log.info(f"[持仓同步] 多 {self._long.qty:.0f}@{entry:.4f} ({diff:+.0f})")
                        if diff > 0:
                            position_grew = True
                elif side == "short":
                    found_short = True
                    self._tp_no_position_until["short"] = 0.0
                    self._position_missing_counts["short"] = 0
                    self._short.cost = position_cost
                    if abs(qty - self._short.qty) > self._position_qty_tolerance():
                        position_changed = True
                        diff = qty - self._short.qty
                        self._short.qty = qty; self._short.entry = entry
                        self._update_position_age("short", qty)
                        if diff > 0:
                            self._short.adds += 1
                        elif diff < -self._position_qty_tolerance():
                            if not hasattr(self, "_normal_close_sides"):
                                self._normal_close_sides = {"long": False, "short": False}
                            self._normal_close_sides["short"] = True
                        self._short_layer = self._estimate_adds(abs(qty), entry)
                        self.log.info(f"[持仓同步] 空 {self._short.qty:.0f}@{entry:.4f} ({diff:+.0f})")
                        if diff > 0:
                            position_grew = True
            if position_changed:
                self._last_position_change_ts = time.time()
                # Reconcile on both growth and shrink. A partial TP fill leaves a
                # smaller live position that must be protected immediately.
                self._init_existing_tp(cancel_existing=False)
            for pos_side, found in (("long", found_long), ("short", found_short)):
                if found:
                    continue
                pos = self._long if pos_side == "long" else self._short
                if pos.qty <= 0:
                    self._position_missing_counts[pos_side] = 0
                    continue
                self._position_missing_counts[pos_side] += 1
                if self._position_missing_counts[pos_side] >= 3:
                    if self._exchange_side_flat_confirmed(pos_side):
                        # 附加防护：本地订单跟踪器仍有活跃挂单时，交易所"无仓位"
                        # 可能是 API 空返回(code=0 data=[])而非真实平仓（见
                        # api_degraded 注释），此时清理状态会造成仓位失管。
                        # 仅在挂单确实清空时才执行清理。
                        _tracker = getattr(self, "_order_tracker", None)
                        _tracker_live = 0
                        if _tracker is not None:
                            try:
                                _tracker_live = int(_tracker.summary().get("total", 0) or 0)
                            except Exception:
                                _tracker_live = 0
                        if _tracker_live > 0:
                            self.log.warning(
                                f"[持仓同步] {pos_side} missing but tracker_live={_tracker_live}; keep local state"
                            )
                            self._position_missing_counts[pos_side] = 0
                        else:
                            self._clear_side_runtime_state(pos_side, reason="exchange_missing_confirmed")
                            self._position_missing_counts[pos_side] = 0
                    else:
                        # API 降级窗口内查询结果不可信：不清理状态、不累积计数，
                        # 避免网络抖动恢复后误判为仓位消失。
                        self._position_missing_counts[pos_side] = 0
        except Exception as _e:
            self.log.warning(f"[持仓同步] 异常: {_e}")

    def _sync_orders(self):
        """??tick?????????????????"""
        if not self._client or self._trading_halted: return
        try:
            orders = self._fetch_live_orders()
            self._update_order_tracker(orders)
            total = len(orders)
            prev = getattr(self, "_prev_order_cnt", -1)
            self._prev_order_cnt = total
            if prev >= 0 and prev != total:
                self.log.info(f"[订单同步] 挂单数: {prev}->{total}")
                for o in orders:
                    s = o.get("side"); ps = o.get("posSide",""); px = o.get("px"); sz = o.get("sz")
                    self.log.info(f"  {s} {ps} {sz}@{px}")
            # Auto-refill grid orders when some are filled
            grid_long = [o for o in orders if o.get("reduceOnly") != "true" and o.get("side") == "buy" and o.get("posSide") == "long"]
            grid_short = [o for o in orders if o.get("reduceOnly") != "true" and o.get("side") == "sell" and o.get("posSide") == "short"]
            target_layers = self._target_grid_layers()
            long_enabled = self._grid_side_enabled("long")
            short_enabled = self._grid_side_enabled("short")
            target_long = target_layers if long_enabled else 0
            target_short = target_layers if short_enabled else 0
            if not self._in_startup_recovery():
                shrink_long_target = target_long if long_enabled else len(grid_long)
                shrink_short_target = target_short if short_enabled else len(grid_short)
                extra_long = max(0, len(grid_long) - shrink_long_target)
                extra_short = max(0, len(grid_short) - shrink_short_target)
                if extra_long > 0:
                    for o in sorted(grid_long, key=lambda x: float(x.get("px", 0) or 0))[:extra_long]:
                        oid = o.get("ordId", "")
                        px = o.get("px", "")
                        if oid:
                            self._cancel_order(oid)
                            self.log.info(f"[网格瘦身] cancel long grid @{px}")
                if extra_short > 0:
                    for o in sorted(grid_short, key=lambda x: float(x.get("px", 0) or 0), reverse=True)[:extra_short]:
                        oid = o.get("ordId", "")
                        px = o.get("px", "")
                        if oid:
                            self._cancel_order(oid)
                            self.log.info(f"[网格瘦身] cancel short grid @{px}")
                            self._short_grid_pause_until = max(self._short_grid_pause_until, time.time() + 18.0)
            need_long = self._grid_side_enabled("long") and len(grid_long) < target_layers
            need_short = self._grid_side_enabled("short") and len(grid_short) < target_layers
            now = time.time()
            if not self._in_startup_recovery() and (need_long or need_short) and now - self._last_sync_rebuild_time >= self._sync_rebuild_cooldown:
                self._last_sync_rebuild_time = now
                self._place_initial_grid()

        except Exception as _e:
            self.log.warning(f"[订单同步] 异常: {_e}")

    def update_leverage(self, lev: int):
        requested = max(1, min(int(safe_float(lev)), int(safe_float(getattr(self, "_base_lev", lev)))))
        if self._client:
            try:
                self._set_leverage(self._symbol, requested, self._margin_mode)
                self._cur_lev = requested
            except Exception as exc:
                self.log.warning(f"[杠杆] 设置失败，保留 {self._cur_lev}x: {exc}")
        else:
            self._cur_lev = requested
