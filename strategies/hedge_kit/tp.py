"""止盈单管理（TP 初始化/覆盖/压缩/重建）。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class TpMixin:
    """止盈单管理（TP 初始化/覆盖/压缩/重建）（47 个方法）。"""

    def _effective_side_tp_ratio(self, side: str, which: str = "first") -> float:
        """Return a side-specific TP floor while preserving legacy defaults.

        The execution path still applies the global MOCA multiplier and fee
        floor.  This helper only selects the side's configured baseline.
        """
        from strategies.hedge_calculations import effective_side_tp_ratio
        return effective_side_tp_ratio(
            getattr(self, "_side_config", {}), side, which,
            self._first_tp, self._final_tp, self._trail,
        )

    def _effective_tp_ratio(self, target_ratio: float, order_mode: str = "maker") -> float:
        from strategies.hedge_calculations import effective_tp_ratio
        return effective_tp_ratio(
            target_ratio, self._min_edge_ratio, order_mode,
            self.TAKER_FEE, self.MAKER_FEE,
        )

    def _check_tp(self, pnl_rate: float, price: float) -> Optional[dict]:
        if pnl_rate >= self._final_tp:
            return self._close_all("final_tp", pnl_rate, price)
        if self._first_tp_done:
            trail_side = "long" if self._long.qty >= self._short.qty else "short"
            trail_enabled = bool(self._side_value(trail_side, "trailing_enabled", True))
            trail_ratio = self._effective_side_tp_ratio(trail_side, "trailing")
            if trail_enabled and self._peak_pnl - pnl_rate >= trail_ratio:
                return self._close_all("trailing", pnl_rate, price)
        if not self._first_tp_done and pnl_rate >= self._first_tp:
            return self._first_tp_exec(price, pnl_rate)
        return None

    def _first_tp_exec(self, price: float, pnl: float) -> dict:
        self._first_tp_done = True
        lq = self._long.qty * 0.5; sq = self._short.qty * 0.5
        contract_value = max(self._ct_val, 1e-12)
        gross = contract_value * (
            lq * (price - self._long.entry) + sq * (self._short.entry - price)
        )
        fee = 0.0; net = 0.0
        if self._client:
            fee = (lq * price + sq * price) * contract_value * self.TAKER_FEE
            net = gross - fee
            self._total_fees += fee; self._total_pnl += net
        self.log.info(f"[首次止盈] gross={gross:.2f}U fee={fee:.4f}U net={net:.2f}U total_pnl={self._total_pnl:.2f}U" + (" (模拟)" if not self._client else ""))
        if net * self._compound > 0:
            self._base_pos_usdt += net * self._compound
        self._save_pnl_data()
        step = self._qty_step
        return {"type":"first_tp","pnl":round(net,2),
            "actions":[
                {"action":"sell","quantity":str(round(lq/step)*step),"pos_side":"long","reduce_only":True},
                {"action":"buy","quantity":str(round(sq/step)*step),"pos_side":"short","reduce_only":True},
            ]}

    def _restore_live_tp_orders(self, orders):
        restored = {"long": 0, "short": 0}
        restored_items = []
        self._layer_tps.clear()
        self._layer_tp_orders.clear()
        self._layer_qty_map.clear()
        self._layer_side_map.clear()
        self._next_layer_id = 0
        for o in orders:
            if o.get("reduceOnly") != "true":
                continue
            pside = o.get("posSide", "")
            px = float(o.get("px", 0) or 0)
            qty = safe_float(o.get("sz", 0))
            oid = o.get("ordId", "")
            if pside not in {"long", "short"} or px <= 0 or qty <= 0 or not oid:
                continue
            pos = self._long if pside == "long" else self._short
            if pos.entry > 0 and ((pside == "long" and px <= pos.entry) or (pside == "short" and px >= pos.entry)):
                self.log.warning(f"[initTP] cancel dangerous live TP {pside} {qty}@{px} entry={pos.entry:.4f} oid={oid}")
                try:
                    self._cancel_order(oid)
                except Exception as e:
                    self.log.warning(f"[initTP] cancel dangerous live TP failed oid={oid}: {e}")
                continue
            lid = str(self._next_layer_id * 2 if pside == "long" else self._next_layer_id * 2 + 1)
            self._next_layer_id += 1
            self._layer_tps[lid] = px
            self._layer_qty_map[lid] = qty
            self._layer_side_map[lid] = pside
            self._layer_tp_orders[lid] = oid
            restored[pside] += 1
            restored_items.append((pside, round(qty, 8), round(px, 8), oid))
        signature = tuple(sorted(restored_items))
        if signature != getattr(self, "_last_restored_tp_signature", None):
            for pside, qty, px, _oid in restored_items:
                self.log.info(f"[initTP] restored live TP {pside} {qty}@{px}")
            self._last_restored_tp_signature = signature
        self._active_tp_orders = restored["long"] + restored["short"]
        return restored

    def _seed_layered_tp_side(self, side: str, pos: Pos, tiers) -> int:
        if pos.qty <= 0 or pos.entry <= 0:
            return 0
        created = 0
        for portion, mult in tiers:
            qty_part = round(pos.qty * portion / self._qty_step) * self._qty_step
            qty_part = max(self._min_qty, min(pos.qty, qty_part))
            if qty_part <= 0:
                continue
            lid = str(self._next_layer_id * 2 if side == "long" else self._next_layer_id * 2 + 1)
            self._next_layer_id += 1
            if side == "long":
                tp_price = pos.entry * (1 + self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * mult, "maker"))
            else:
                tp_price = pos.entry * (1 - self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * mult, "maker"))
            self._layer_tps[lid] = tp_price
            self._layer_qty_map[lid] = qty_part
            self._layer_side_map[lid] = side
            if self._place_tp_order(lid, side, qty_part, tp_price):
                created += 1
            else:
                self._layer_tps.pop(lid, None)
                self._layer_qty_map.pop(lid, None)
                self._layer_side_map.pop(lid, None)
        return created

    def _tp_lid_side(self, lid: str) -> str:
        side = self._layer_side_map.get(str(lid), "")
        if side in {"long", "short"}:
            return side
        return "long" if str(lid).isdigit() and int(lid) % 2 == 0 else "short"

    def _drop_tp_state_for_lids(self, lids) -> None:
        """Remove local TP mappings whose exchange orders are no longer live."""
        for lid in {str(value) for value in (lids or [])}:
            self._layer_tp_orders.pop(lid, None)
            self._layer_tps.pop(lid, None)
            self._layer_qty_map.pop(lid, None)
            self._layer_side_map.pop(lid, None)
            self._tp_rehang_state.pop(lid, None)
        self._active_tp_orders = len(self._layer_tp_orders)

    def _drop_tp_state_for_order_ids(self, order_ids) -> None:
        ids = {str(value) for value in (order_ids or []) if str(value)}
        if not ids:
            return
        lids = [lid for lid, oid in self._layer_tp_orders.items() if str(oid) in ids]
        self._drop_tp_state_for_lids(lids)

    def _tp_covered_qty(self, side: str) -> float:
        return sum(
            safe_float(qty)
            for lid, qty in self._layer_qty_map.items()
            if self._tp_lid_side(lid) == side
        )

    @staticmethod
    def _tp_coverage_from_orders(orders) -> dict:
        coverage = {"long": 0.0, "short": 0.0}
        for order in orders or []:
            if str(order.get("reduceOnly", "")).lower() != "true":
                continue
            side = str(order.get("posSide") or "").lower()
            if side in coverage:
                coverage[side] += abs(safe_float(order.get("sz", 0)))
        return coverage

    def _update_exchange_tp_coverage(self, orders) -> dict:
        coverage = self._tp_coverage_from_orders(orders)
        self._exchange_tp_coverage_qty = coverage
        self._exchange_tp_coverage_ready = True
        self._exchange_tp_coverage_at = time.time()
        return coverage

    def _deduplicate_live_tp_orders(self, live_orders):
        """Cancel exact duplicate reduce-only TP orders, preserving one oldest order."""
        groups = {}
        for order in live_orders or []:
            if str(order.get("reduceOnly", "")).lower() != "true":
                continue
            side = str(order.get("posSide") or "").lower()
            order_side = str(order.get("side") or "").lower()
            oid = str(order.get("ordId") or "")
            price = safe_float(order.get("px", 0))
            if side not in {"long", "short"} or order_side not in {"buy", "sell"} or not oid or price <= 0:
                continue
            groups.setdefault((side, order_side, round(price, 12)), []).append(order)
        duplicate_ids = []
        for orders in groups.values():
            if len(orders) <= 1:
                continue
            ordered = sorted(orders, key=lambda item: safe_float(item.get("uTime") or item.get("cTime") or 0))
            duplicate_ids.extend(str(item.get("ordId") or "") for item in ordered[1:])
        if not duplicate_ids:
            return live_orders
        for oid in duplicate_ids:
            try:
                self._cancel_order(oid, self._symbol)
            except Exception as exc:
                self.log.warning(f"[TP dedup] cancel failed oid={oid}: {exc}")
        try:
            return self._fetch_live_orders()
        except Exception:
            duplicate_set = set(duplicate_ids)
            return [order for order in live_orders if str(order.get("ordId") or "") not in duplicate_set]

    def _tp_coverage_tolerance(self, side: str, position_qty: float = 0.0) -> float:
        hard = self._position_qty_tolerance()
        if side not in {"long", "short"} or not bool(getattr(self, "_exchange_tp_coverage_ready", False)):
            return hard
        position_qty = safe_float(position_qty)
        covered = self._risk_tp_covered_qty(side)
        if position_qty <= 0 or covered <= 0 or covered / position_qty < 0.90:
            return hard
        # Only exchange rounding is tolerated. Larger gaps are small enough
        # to place as safety TP and must be repaired before new exposure.
        return max(hard, 0.51 * safe_float(getattr(self, "_qty_step", 0.0)))

    def _coverage_safety_tp_allowed(self, side: str, qty: float, price: float, pos: Pos) -> bool:
        if not bool(getattr(self, "_coverage_safety_tp_enabled", False)):
            return False
        if side not in {"long", "short"} or qty <= 0 or price <= 0 or safe_float(pos.qty) <= 0:
            return False
        max_gap = safe_float(getattr(self, "_coverage_safety_tp_max_gap_ratio", 0.10))
        if max_gap <= 0 or qty > pos.qty * max_gap + self._position_qty_tolerance():
            return False
        if side == "long" and price <= pos.entry:
            return False
        if side == "short" and price >= pos.entry:
            return False
        expected = self._tp_expected_net(side, qty, price, pos.entry)
        return expected + safe_float(getattr(self, "_coverage_safety_tp_max_loss_usdt", 0.05)) >= -1e-8

    def _load_coverage_safety_tp_state(self) -> None:
        path = str(getattr(self, "_coverage_safety_tp_state_file", "") or "")
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError("state is not an object")
            limit = max(0, int(safe_float(getattr(self, "_coverage_safety_tp_repair_limit", 0))))
            incidents = data.get("incidents") if isinstance(data.get("incidents"), dict) else {}
            remaining = data.get("remaining") if isinstance(data.get("remaining"), dict) else {}
            quantities = data.get("position_qty") if isinstance(data.get("position_qty"), dict) else {}
            for side in ("long", "short"):
                qty = max(0.0, safe_float(quantities.get(side, 0.0)))
                incident = bool(incidents.get(side, False)) and qty > 0
                self._coverage_safety_tp_repair_incident[side] = incident
                self._coverage_safety_tp_repair_position_qty[side] = qty
                self._coverage_safety_tp_repair_remaining[side] = min(limit, max(0, int(safe_float(remaining.get(side, 0))))) if incident else 0
        except Exception as exc:
            self.log.warning(f"[coverage safety] invalid state reset: {exc}")

    def _save_coverage_safety_tp_state(self) -> None:
        path = str(getattr(self, "_coverage_safety_tp_state_file", "") or "")
        if not path:
            return
        temp_path = ""
        try:
            target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "symbol": self._symbol, "profile_slug": self._profile_slug,
                       "incidents": dict(self._coverage_safety_tp_repair_incident),
                       "remaining": dict(self._coverage_safety_tp_repair_remaining),
                       "position_qty": dict(self._coverage_safety_tp_repair_position_qty)}
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp", delete=False) as handle:
                temp_path = handle.name; json.dump(payload, handle, indent=2, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp_path, path); temp_path = ""
        except Exception as exc:
            self.log.warning(f"[coverage safety] state save failed: {exc}")
        finally:
            if temp_path:
                try: os.unlink(temp_path)
                except OSError: pass

    def _reset_coverage_safety_tp_incident_if_reduced(self, side: str, position_qty: float) -> None:
        if side not in {"long", "short"}: return
        quantities = getattr(self, "_coverage_safety_tp_repair_position_qty", {})
        incidents = getattr(self, "_coverage_safety_tp_repair_incident", {})
        remaining = getattr(self, "_coverage_safety_tp_repair_remaining", {})
        previous = safe_float(quantities.get(side, 0.0)) if isinstance(quantities, dict) else 0.0
        if previous <= 0 or position_qty + self._position_qty_tolerance() >= previous: return
        if isinstance(incidents, dict): incidents[side] = False
        if isinstance(remaining, dict): remaining[side] = 0
        if isinstance(quantities, dict): quantities[side] = max(0.0, position_qty)
        self._save_coverage_safety_tp_state()

    def _reset_coverage_safety_tp_incident_if_grown(self, side: str, position_qty: float) -> None:
        """Start a fresh bounded coverage incident after inventory grows.

        A prior safety TP allowance is tied to the position size that caused
        that incident.  If the position later grows, keeping the old exhausted
        allowance can leave only the incremental quantity permanently
        uncovered and block all future opening.  Growth starts a new incident;
        the normal TP price/profit checks remain authoritative.
        """
        if side not in {"long", "short"}:
            return
        quantities = getattr(self, "_coverage_safety_tp_repair_position_qty", {})
        incidents = getattr(self, "_coverage_safety_tp_repair_incident", {})
        remaining = getattr(self, "_coverage_safety_tp_repair_remaining", {})
        if not isinstance(quantities, dict) or not isinstance(incidents, dict) or not isinstance(remaining, dict):
            return
        previous = safe_float(quantities.get(side, 0.0))
        current = max(0.0, safe_float(position_qty))
        if previous <= 0.0 or current <= previous + self._position_qty_tolerance():
            return
        incidents[side] = False
        remaining[side] = 0
        quantities[side] = current
        self._save_coverage_safety_tp_state()

    def _register_coverage_safety_tp(self, side: str, position_qty: float) -> None:
        if side not in {"long", "short"}: return
        self._reset_coverage_safety_tp_incident_if_reduced(side, position_qty)
        quantities = getattr(self, "_coverage_safety_tp_repair_position_qty", None)
        incidents = getattr(self, "_coverage_safety_tp_repair_incident", None)
        remaining = getattr(self, "_coverage_safety_tp_repair_remaining", None)
        if not isinstance(quantities, dict): quantities = {"long": 0.0, "short": 0.0}; self._coverage_safety_tp_repair_position_qty = quantities
        if not isinstance(incidents, dict): incidents = {"long": False, "short": False}; self._coverage_safety_tp_repair_incident = incidents
        if not isinstance(remaining, dict): remaining = {"long": 0, "short": 0}; self._coverage_safety_tp_repair_remaining = remaining
        current = safe_float(quantities.get(side, 0.0)); quantities[side] = max(current, max(0.0, position_qty))
        if incidents.get(side):
            self._save_coverage_safety_tp_state(); return
        incidents[side] = True
        remaining[side] = max(0, int(getattr(self, "_coverage_safety_tp_repair_limit", 1)))
        self._save_coverage_safety_tp_state()

    def _coverage_safety_tp_repair_blocked(self, side: str) -> bool:
        incidents = getattr(self, "_coverage_safety_tp_repair_incident", {})
        remaining = getattr(self, "_coverage_safety_tp_repair_remaining", {})
        return isinstance(incidents, dict) and bool(incidents.get(side)) and safe_float(remaining.get(side, 0)) <= 0

    def _consume_coverage_safety_tp_repair(self, side: str) -> None:
        remaining = getattr(self, "_coverage_safety_tp_repair_remaining", {})
        if isinstance(remaining, dict) and side in remaining:
            current = max(0, int(safe_float(remaining.get(side, 0))))
            remaining[side] = max(0, current - 1)
            if current != remaining[side]: self._save_coverage_safety_tp_state()

    def _risk_tp_covered_qty(self, side: str) -> float:
        if bool(getattr(self, "_exchange_tp_coverage_ready", False)):
            coverage = getattr(self, "_exchange_tp_coverage_qty", {})
            return max(0.0, safe_float(coverage.get(side, 0.0)))
        return self._tp_covered_qty(side)

    def _tp_covered_other_qty(
        self, side: str, exclude_lid: str, live_qty: float, tolerance: float,
        exclude_qty: float = 0.0,
    ) -> float:
        """Coverage already held by *other* TP layers on this side.

        A layer entry with no exchange order id never reached the book: it is a
        reservation dropped mid-flight or a cancelled/filled order whose local
        mapping survived. Counting those phantom layers as coverage drives the
        available quantity to zero, so every replacement TP fails with
        qty_below_min, coverage stays empty, and tp_coverage_incomplete blocks
        every add on that side. Only layers backed by a live order id count.

        The exchange coverage view floors the result so discarding phantoms can
        never understate coverage and over-submit reduce-only quantity.

        ``exclude_qty`` subtracts the quantity of the layer being *replaced*
        (rehang): its old order is mid-cancellation and may still be counted by
        the exchange cache. Without it a rehang replace sees the old order as
        live coverage, driving available_qty to zero and every replacement TP
        fails with qty_below_min. Seeding/supplement pass 0 because the lid is
        new and not yet on the book.
        """
        covered_other = 0.0
        phantom_qty = 0.0
        for layer_lid, layer_qty in self._layer_qty_map.items():
            if str(layer_lid) == str(exclude_lid) or self._tp_lid_side(layer_lid) != side:
                continue
            layer_abs = abs(safe_float(layer_qty))
            if str(self._layer_tp_orders.get(layer_lid, "") or ""):
                covered_other += layer_abs
            else:
                phantom_qty += layer_abs
        exclude_qty = max(0.0, safe_float(exclude_qty))
        if bool(getattr(self, "_exchange_tp_coverage_ready", False)):
            exchange_covered = min(
                max(0.0, self._risk_tp_covered_qty(side) - exclude_qty),
                live_qty,
            )
            covered_other = max(covered_other, exchange_covered)
        if phantom_qty > tolerance:
            self.log.warning(
                f"[TP] phantom coverage ignored side={side} phantom={phantom_qty:.8f} "
                f"covered={covered_other:.8f} live={live_qty:.8f}"
            )
        return covered_other

    def _tp_coverage_block_reason(self, side: str) -> str:
        """Block additions only after live TP state has been initialized."""
        if side not in {"long", "short"} or not getattr(self, "_tp_init_done", False):
            return ""
        position = self._long if side == "long" else self._short
        if position.qty <= 0:
            return ""
        tolerance = self._tp_coverage_tolerance(side, position.qty)
        if self._risk_tp_covered_qty(side) + tolerance < position.qty:
            return "tp_coverage_incomplete"
        return ""

    def _tp_expected_net(self, side: str, qty: float, price: float, entry: float) -> float:
        """Expected PnL after conservative open, close, and funding costs."""
        qty = max(0.0, safe_float(qty))
        price = safe_float(price)
        entry = safe_float(entry)
        contract_value = max(safe_float(getattr(self, "_ct_val", 1.0)), 1e-12)
        if qty <= 0 or price <= 0 or entry <= 0 or side not in {"long", "short"}:
            return 0.0
        gross = qty * contract_value * (
            price - entry if side == "long" else entry - price
        )
        open_fee = qty * contract_value * entry * self.TAKER_FEE
        close_fee = qty * contract_value * price * self.MAKER_FEE
        funding = (
            qty
            * contract_value
            * entry
            * max(0.0, safe_float(getattr(self, "_tp_funding_buffer_ratio", 0.001)))
        )
        return gross - open_fee - close_fee - funding

    def _tp_orders_expected_net(self, side: str, orders, entry: float) -> float:
        return sum(
            self._tp_expected_net(
                side,
                safe_float(order.get("sz", order.get("qty", 0))),
                safe_float(order.get("px", order.get("price", 0))),
                entry,
            )
            for order in orders
        )

    def _round_tp_price_away(self, side: str, price: float) -> float:
        from strategies.hedge_calculations import round_tp_price_away
        return round_tp_price_away(side, price, safe_float(getattr(self, "_tick_size", 0.0)))

    def _minimum_profitable_tp_price(self, side: str, qty: float, entry: float) -> float:
        """Price that leaves the configured USDT buffer after all reserved costs."""
        contract_qty = qty * max(safe_float(getattr(self, "_ct_val", 1.0)), 1e-12)
        if contract_qty <= 0 or entry <= 0:
            return 0.0
        funding_ratio = max(
            0.0, safe_float(getattr(self, "_tp_funding_buffer_ratio", 0.001))
        )
        buffer_usdt = max(0.0, safe_float(getattr(self, "_min_expected_net_usdt", 0.0)))
        if side == "long":
            price = (
                buffer_usdt / contract_qty
                + entry * (1.0 + self.TAKER_FEE + funding_ratio)
            ) / (1.0 - self.MAKER_FEE)
        else:
            price = (
                entry * (1.0 - self.TAKER_FEE - funding_ratio)
                - buffer_usdt / contract_qty
            ) / (1.0 + self.MAKER_FEE)
        return self._round_tp_price_away(side, price)

    def _tp_compaction_plan(self, live_orders, positions):
        tp_orders = [
            order for order in live_orders
            if str(order.get("reduceOnly", "")).lower() == "true"
            and str(order.get("posSide", "")).lower() in {"long", "short"}
            and safe_float(order.get("px", 0)) > 0
            and safe_float(order.get("sz", 0)) > 0
            and str(order.get("ordId", ""))
        ]
        target = min(
            max(1, int(safe_float(getattr(self, "_tp_compact_target_orders", 5)))),
            max(1, int(safe_float(getattr(self, "_max_tp_orders", 5)))),
        )
        if len(tp_orders) <= target:
            return None
        tolerance = max(self._qty_step * 0.51, 1e-8)
        by_side = {"long": [], "short": []}
        for order in tp_orders:
            by_side[str(order.get("posSide", "")).lower()].append(order)
        for side, orders in by_side.items():
            covered = sum(safe_float(order.get("sz", 0)) for order in orders)
            if abs(covered - positions[side].qty) > tolerance:
                self.log.warning(
                    f"[TP compact] skip incomplete coverage side={side} "
                    f"orders={covered} position={positions[side].qty}"
                )
                return None
        market = safe_float(getattr(self, "_last_price", 0.0))
        if market <= 0:
            return None
        protected_count = min(2, target - 1, len(tp_orders))
        protected = sorted(
            tp_orders,
            key=lambda order: abs(safe_float(order.get("px", 0)) - market),
        )[:protected_count]
        protected_ids = {str(order.get("ordId", "")) for order in protected}
        remote_by_side = {
            side: [
                order for order in orders
                if str(order.get("ordId", "")) not in protected_ids
            ]
            for side, orders in by_side.items()
        }
        remote_sides = [side for side, orders in remote_by_side.items() if orders]
        if len(protected) + len(remote_sides) > target:
            return None
        return {
            "target": target,
            "protected_ids": protected_ids,
            "remote_by_side": remote_by_side,
            "old_by_side": by_side,
        }

    def _allocate_tp_compaction_slots(self, source_by_side, missing_by_side, slots: int):
        active = [
            side for side in ("long", "short")
            if missing_by_side.get(side, 0.0) >= self._min_qty and source_by_side.get(side)
        ]
        if len(active) > slots:
            return None
        allocation = {side: 1 for side in active}
        remaining = slots - len(active)
        while remaining > 0:
            eligible = [
                side for side in active
                if allocation[side] < len(source_by_side[side])
                and allocation[side] * self._min_qty < missing_by_side[side] + 1e-8
            ]
            if not eligible:
                break
            side = max(
                eligible,
                key=lambda item: (
                    len(source_by_side[item]) - allocation[item],
                    missing_by_side[item],
                ),
            )
            allocation[side] += 1
            remaining -= 1
        return allocation

    def _build_remote_tp_buckets(self, side: str, source_orders, qty: float, slots: int, entry: float):
        source = sorted(source_orders, key=lambda order: safe_float(order.get("px", 0)))
        source = [order for order in source if safe_float(order.get("sz", 0)) > 0]
        total_units = int(round(qty / self._qty_step))
        slots = min(slots, len(source), total_units)
        if slots <= 0:
            return []
        groups = [[] for _ in range(slots)]
        for index, order in enumerate(source):
            group_index = min(slots - 1, int(index * slots / len(source)))
            groups[group_index].append(order)
        group_qty = [sum(safe_float(order.get("sz", 0)) for order in group) for group in groups]
        source_qty = sum(group_qty)
        raw_units = [total_units * value / source_qty for value in group_qty]
        units = [max(1, int(math.floor(value))) for value in raw_units]
        while sum(units) > total_units:
            index = max(
                (i for i, value in enumerate(units) if value > 1),
                key=lambda i: units[i] - raw_units[i],
            )
            units[index] -= 1
        while sum(units) < total_units:
            index = max(range(slots), key=lambda i: raw_units[i] - units[i])
            units[index] += 1
        result = []
        for group, unit_count in zip(groups, units):
            bucket_qty = unit_count * self._qty_step
            weighted_price = sum(
                safe_float(order.get("px", 0)) * safe_float(order.get("sz", 0))
                for order in group
            ) / sum(safe_float(order.get("sz", 0)) for order in group)
            minimum_price = self._minimum_profitable_tp_price(side, bucket_qty, entry)
            if side == "long":
                price = max(weighted_price, minimum_price)
            else:
                price = min(weighted_price, minimum_price)
            price = self._round_tp_price_away(side, price)
            if price <= 0:
                return []
            result.append({"sz": bucket_qty, "px": price})

        source_net_per_qty = self._tp_orders_expected_net(side, source, entry) / source_qty
        required_net = source_net_per_qty * qty
        new_net = self._tp_orders_expected_net(side, result, entry)
        deficit = required_net - new_net
        if deficit > 1e-8:
            index = max(range(len(result)), key=lambda i: safe_float(result[i]["sz"]))
            bucket_qty = safe_float(result[index]["sz"])
            contract_qty = bucket_qty * max(safe_float(getattr(self, "_ct_val", 1.0)), 1e-12)
            slope = contract_qty * (1.0 - self.MAKER_FEE if side == "long" else 1.0 + self.MAKER_FEE)
            delta = deficit / max(slope, 1e-12)
            result[index]["px"] = self._round_tp_price_away(
                side,
                safe_float(result[index]["px"]) + (delta if side == "long" else -delta),
            )
        buffer_usdt = max(0.0, safe_float(getattr(self, "_min_expected_net_usdt", 0.0)))
        if any(
            self._tp_expected_net(side, order["sz"], order["px"], entry) + 1e-8 < buffer_usdt
            for order in result
        ):
            return []
        if self._tp_orders_expected_net(side, result, entry) + 1e-8 < required_net:
            return []
        return result

    def _raise_tp_plan_profit(self, side: str, orders, deficit: float) -> bool:
        if deficit <= 1e-8 or not orders:
            return True
        index = max(range(len(orders)), key=lambda i: safe_float(orders[i].get("sz", 0)))
        qty = safe_float(orders[index].get("sz", 0))
        contract_qty = qty * max(safe_float(getattr(self, "_ct_val", 1.0)), 1e-12)
        slope = contract_qty * (
            1.0 - self.MAKER_FEE if side == "long" else 1.0 + self.MAKER_FEE
        )
        if slope <= 0:
            return False
        delta = deficit / slope
        price = safe_float(orders[index].get("px", 0))
        orders[index]["px"] = self._round_tp_price_away(
            side, price + (delta if side == "long" else -delta)
        )
        return safe_float(orders[index]["px"]) > 0

    def _finish_tp_compaction_once(self, plan):
        try:
            live_orders = self._fetch_live_orders()
            positions = self._fetch_exchange_positions()
        except Exception as error:
            self.log.error(f"[TP compact] post-cancel refresh failed: {error}")
            return None
        self._apply_exchange_positions(positions)
        live_tp = [
            order for order in live_orders
            if str(order.get("reduceOnly", "")).lower() == "true"
        ]
        # The compaction plan cancels remote TP orders before placing their
        # replacements. Once the exchange confirms an order is gone, remove
        # its local quantity mapping as well; otherwise _place_tp_order treats
        # the cancelled quantity as still covered and clips the replacement to
        # zero.
        remote_ids = {
            str(order.get("ordId", ""))
            for side in ("long", "short")
            for order in plan["remote_by_side"].get(side, [])
            if str(order.get("ordId", ""))
        }
        live_ids = {
            str(order.get("ordId", "")) for order in live_tp if str(order.get("ordId", ""))
        }
        self._drop_tp_state_for_order_ids(remote_ids - live_ids)
        # Rebuild the local TP map from the exchange-confirmed live orders.
        # A failed/cancelled compaction can leave a layer quantity without a
        # matching order id; counting that stale quantity makes the next
        # replacement look fully covered and clips its size to zero.
        self._restore_live_tp_orders(live_orders)
        target = int(plan["target"])
        if len(live_tp) > target:
            remote_ids = {
                str(order.get("ordId", ""))
                for side in ("long", "short")
                for order in plan["remote_by_side"].get(side, [])
            }
            for order in live_tp:
                oid = str(order.get("ordId", ""))
                if oid not in remote_ids:
                    continue
                try:
                    self._cancel_order(oid)
                except Exception as error:
                    self.log.warning(f"[TP compact] retry cancel failed oid={oid}: {error}")
            live_orders = self._fetch_live_orders()
            live_tp = [
                order for order in live_orders
                if str(order.get("reduceOnly", "")).lower() == "true"
            ]
            live_ids = {
                str(order.get("ordId", "")) for order in live_tp if str(order.get("ordId", ""))
            }
            self._drop_tp_state_for_order_ids(remote_ids - live_ids)
            self._restore_live_tp_orders(live_orders)
        if len(live_tp) > target:
            self.log.warning(
                f"[TP compact] cancellation incomplete live={len(live_tp)} target={target}; retry later"
            )
            return None
        dangerous_ids = []
        for order in live_tp:
            side = str(order.get("posSide", "")).lower()
            if side not in positions or positions[side].qty <= 0:
                continue
            price = safe_float(order.get("px", 0))
            if (
                (side == "long" and price <= positions[side].entry)
                or (side == "short" and price >= positions[side].entry)
            ):
                dangerous_ids.append(str(order.get("ordId", "")))
        if dangerous_ids:
            self.log.warning(
                f"[TP compact] latest entry invalidated retained TP; replace ids={dangerous_ids}"
            )
            for oid in dangerous_ids:
                try:
                    self._cancel_order(oid)
                except Exception as error:
                    self.log.warning(f"[TP compact] dangerous TP cancel failed oid={oid}: {error}")
            refreshed_ids = {
                str(order.get("ordId", "")) for order in self._fetch_live_orders()
            }
            if any(oid in refreshed_ids for oid in dangerous_ids):
                self.log.error("[TP compact] dangerous retained TP still live; retry later")
                return None
            return self._finish_tp_compaction(plan)
        retained_by_side = {"long": [], "short": []}
        for order in live_tp:
            side = str(order.get("posSide", "")).lower()
            if side in retained_by_side:
                retained_by_side[side].append(order)
        tolerance = max(self._qty_step * 0.51, 1e-8)
        missing_by_side = {}
        for side in ("long", "short"):
            retained_qty = sum(
                safe_float(order.get("sz", 0)) for order in retained_by_side[side]
            )
            if retained_qty > positions[side].qty + tolerance:
                self.log.error(
                    f"[TP compact] retained coverage exceeds latest position side={side} "
                    f"orders={retained_qty} position={positions[side].qty}"
                )
                return None
            missing_by_side[side] = round_step(
                max(0.0, positions[side].qty - retained_qty), self._qty_step
            )
        allocation = self._allocate_tp_compaction_slots(
            plan["remote_by_side"], missing_by_side, target - len(live_tp)
        )
        if allocation is None:
            self.log.error("[TP compact] insufficient slots for latest position coverage")
            return None
        replacements = {"long": [], "short": []}
        for side, slots in allocation.items():
            replacements[side] = self._build_remote_tp_buckets(
                side,
                plan["remote_by_side"][side],
                missing_by_side[side],
                slots,
                positions[side].entry,
            )
            planned_qty = sum(safe_float(order.get("sz", 0)) for order in replacements[side])
            if abs(planned_qty - missing_by_side[side]) > tolerance:
                self.log.error(
                    f"[TP compact] could not build safe replacement side={side} "
                    f"planned={planned_qty} missing={missing_by_side[side]}"
                )
                return None

        for side in ("long", "short"):
            old_orders = plan["old_by_side"].get(side, [])
            old_qty = sum(safe_float(order.get("sz", 0)) for order in old_orders)
            if positions[side].qty <= 0 or old_qty <= 0:
                continue
            old_reference = (
                self._tp_orders_expected_net(side, old_orders, positions[side].entry)
                / old_qty
                * positions[side].qty
            )
            new_expected = self._tp_orders_expected_net(
                side, retained_by_side[side] + replacements[side], positions[side].entry
            )
            if not self._raise_tp_plan_profit(
                side, replacements[side], old_reference - new_expected
            ):
                return None
            new_expected = self._tp_orders_expected_net(
                side, retained_by_side[side] + replacements[side], positions[side].entry
            )
            if new_expected + 1e-8 < old_reference:
                self.log.error(
                    f"[TP compact] profit invariant failed before placement side={side} "
                    f"old={old_reference:.8f} new={new_expected:.8f}"
                )
                return None
            self.log.info(
                f"[TP compact] side={side} latest_entry={positions[side].entry:.8f} "
                f"old_expected_net={old_reference:.8f} new_expected_net={new_expected:.8f}"
            )

        placed_oids = []
        for side in ("long", "short"):
            for order in replacements[side]:
                lid = str(self._next_layer_id * 2 if side == "long" else self._next_layer_id * 2 + 1)
                self._next_layer_id += 1
                self._layer_tps[lid] = safe_float(order["px"])
                self._layer_qty_map[lid] = safe_float(order["sz"])
                self._layer_side_map[lid] = side
                placed = self._place_tp_order(lid, side, order["sz"], order["px"])
                oid = self._layer_tp_orders.get(lid, "")
                if not placed or not oid:
                    failure = getattr(self, "_last_tp_place_failure", {}) or {}
                    reason = str(failure.get("reason") or "unknown")
                    self._drop_tp_state_for_lids([lid])
                    context = (
                        f"qty={safe_float(order.get('sz', 0)):.8f} "
                        f"price={safe_float(order.get('px', 0)):.8f} "
                        f"live_qty={safe_float(positions[side].qty):.8f} "
                        f"live_tp={len(live_tp)}"
                    )
                    message = f"[TP compact] replacement placement failed side={side} reason={reason} {context}"
                    if reason in {"exchange_flat", "no_position_backoff", "qty_below_min", "dangerous_tp"}:
                        self.log.warning(message)
                    else:
                        self.log.error(message)
                    return None
                placed_oids.append(oid)

        try:
            final_orders = self._fetch_live_orders()
            final_positions = self._fetch_exchange_positions()
        except Exception as error:
            self.log.error(f"[TP compact] final verification read failed: {error}")
            return None
        final_tp = [
            order for order in final_orders
            if str(order.get("reduceOnly", "")).lower() == "true"
        ]
        final_by_side = {"long": [], "short": []}
        dangerous_final_ids = []
        for order in final_tp:
            side = str(order.get("posSide", "")).lower()
            if side in final_by_side:
                final_by_side[side].append(order)
        valid = len(final_tp) <= target
        for side in ("long", "short"):
            position = final_positions[side]
            covered = sum(safe_float(order.get("sz", 0)) for order in final_by_side[side])
            old_orders = plan["old_by_side"].get(side, [])
            old_qty = sum(safe_float(order.get("sz", 0)) for order in old_orders)
            reference = 0.0
            if old_qty > 0 and position.qty > 0:
                reference = (
                    self._tp_orders_expected_net(side, old_orders, position.entry)
                    / old_qty
                    * position.qty
                )
            final_expected = self._tp_orders_expected_net(
                side, final_by_side[side], position.entry
            )
            dangerous = any(
                (side == "long" and safe_float(order.get("px", 0)) <= position.entry)
                or (side == "short" and safe_float(order.get("px", 0)) >= position.entry)
                for order in final_by_side[side]
                if position.qty > 0
            )
            if dangerous:
                dangerous_final_ids.extend(
                    str(order.get("ordId", ""))
                    for order in final_by_side[side]
                    if (
                        (side == "long" and safe_float(order.get("px", 0)) <= position.entry)
                        or (side == "short" and safe_float(order.get("px", 0)) >= position.entry)
                    )
                )
            if (
                abs(covered - position.qty) > tolerance
                or dangerous
                or final_expected + 1e-8 < reference
            ):
                valid = False
                self.log.error(
                    f"[TP compact] final invariant failed side={side} coverage={covered}/{position.qty} "
                    f"dangerous={dangerous} old={reference:.8f} new={final_expected:.8f}"
                )
        if not valid:
            for oid in set(placed_oids + dangerous_final_ids):
                try:
                    self._cancel_order(oid)
                except Exception as error:
                    self.log.warning(f"[TP compact] rollback new order failed oid={oid}: {error}")
            return None
        self._apply_exchange_positions(final_positions)
        self._tp_compaction_pending = None
        self._last_tp_compact_ts = time.time()
        self._tp_compact_candidate_signature = None
        self._tp_compact_candidate_since = 0.0
        self.log.info(
            f"[TP compact] completed protected={len(plan['protected_ids'])} "
            f"live={len(final_tp)} target={target}"
        )
        return final_orders

    def _finish_tp_compaction(self, plan):
        now = time.time()
        retry_after = safe_float(getattr(self, "_tp_compaction_retry_after", 0.0))
        if now < retry_after:
            return None
        result = self._finish_tp_compaction_once(plan)
        if result is None:
            failures = int(getattr(self, "_tp_compaction_failures", 0)) + 1
            self._tp_compaction_failures = failures
            delay = min(900.0, 60.0 * (2 ** min(failures - 1, 4)))
            self._tp_compaction_retry_after = now + delay
            self.log.warning(
                f"[TP compact] replacement deferred after failure; retry in {delay:.0f}s"
            )
        else:
            self._tp_compaction_failures = 0
            self._tp_compaction_retry_after = 0.0
        return result

    def _compact_fragmented_tp_orders(self, live_orders, reason: str):
        try:
            positions = self._fetch_exchange_positions()
        except Exception as error:
            self.log.error(f"[TP compact] pre-cancel position read failed: {error}")
            return None
        plan = self._tp_compaction_plan(live_orders, positions)
        if not plan:
            return None
        self._tp_compaction_pending = plan
        self._tp_compaction_failures = 0
        self._tp_compaction_retry_after = 0.0
        self.log.warning(
            f"[TP compact] reason={reason} protect={sorted(plan['protected_ids'])}; cancel remote only"
        )
        for side in ("long", "short"):
            for order in plan["remote_by_side"][side]:
                oid = str(order.get("ordId", ""))
                try:
                    self._cancel_order(oid)
                except Exception as error:
                    self.log.warning(f"[TP compact] cancel failed oid={oid}: {error}")
        return self._finish_tp_compaction(plan)

    def _supplement_tp_coverage_side(self, side: str, pos: Pos) -> int:
        if pos.qty <= 0 or pos.entry <= 0 or not self._client:
            return 0
        self._reset_coverage_safety_tp_incident_if_reduced(side, pos.qty)
        self._reset_coverage_safety_tp_incident_if_grown(side, pos.qty)
        slots = max(0, self._max_tp_orders - self._active_tp_orders)
        exchange_covered = self._risk_tp_covered_qty(side) if bool(getattr(self, "_exchange_tp_coverage_ready", False)) else 0.0
        covered = max(exchange_covered, self._tp_covered_qty(side))
        coverage_gap = max(0.0, pos.qty - covered)
        if coverage_gap <= self._tp_coverage_tolerance(side, pos.qty) + 1e-9:
            return 0
        missing = round(coverage_gap / self._qty_step) * self._qty_step
        if missing < self._min_qty:
            return 0
        existing_prices = [safe_float(tp) for lid, tp in self._layer_tps.items() if self._tp_lid_side(lid) == side]
        if existing_prices:
            anchor_price = min(existing_prices) if side == "long" else max(existing_prices)
            tick = max(self._tick_size, 1e-12)
            repair_price = anchor_price - tick if side == "long" else anchor_price + tick
            minimum_price = self._minimum_profitable_tp_price(side, missing, pos.entry)
            # Try the nearest profitable price first. If the anchor is too
            # close to an existing order or is no longer profitable after
            # rounding, fall back to the exact conservative profit floor and
            # move one tick farther away until the exchange price is unique.
            candidates = [repair_price]
            candidates.append(max(repair_price, minimum_price) if side == "long" else min(repair_price, minimum_price))
            for candidate in candidates:
                candidate = self._round_tp_price_away(side, candidate)
                direction = 1.0 if side == "long" else -1.0
                for _ in range(64):
                    if all(abs(candidate - px) > tick * 0.5 for px in existing_prices):
                        break
                    candidate = round(candidate + direction * tick, 12)
                if candidate <= 0:
                    continue
                profitable = candidate >= minimum_price if side == "long" else candidate <= minimum_price
                coverage_safety = (not profitable and self._coverage_safety_tp_allowed(side, missing, candidate, pos))
                if not (profitable or coverage_safety):
                    continue
                lid = str(self._next_layer_id * 2 if side == "long" else self._next_layer_id * 2 + 1); self._next_layer_id += 1
                self._layer_tps[lid] = candidate; self._layer_qty_map[lid] = missing; self._layer_side_map[lid] = side
                kwargs = {"coverage_safety": True} if coverage_safety else {}
                if self._place_tp_order(lid, side, missing, candidate, **kwargs):
                    self._active_tp_orders = len(self._layer_tp_orders)
                    if coverage_safety: self._register_coverage_safety_tp(side, pos.qty)
                    return 1
                self._layer_tps.pop(lid, None); self._layer_qty_map.pop(lid, None); self._layer_side_map.pop(lid, None)
            self.log.warning(
                f"[initTP] unable to supplement {side} coverage missing={missing:.8f} "
                f"minimum={minimum_price:.8f} last_failure={getattr(self, '_last_tp_place_failure', {})}"
            )
            return 0
        force_compact_safety = False
        if slots <= 0:
            # Coverage is a harder invariant than the layered-TP order cap. If
            # restored live TP orders already exceed the cap, place exactly one
            # compact reduce-only safety TP for the entire uncovered side.
            force_compact_safety = self._active_tp_orders >= self._max_tp_orders_hard
            slots = 1
            if force_compact_safety:
                self.log.error(
                    f"[initTP] hard TP cap reached active={self._active_tp_orders}/"
                    f"{self._max_tp_orders_hard}; creating compact safety {side} TP for missing={missing}"
                )
            else:
                self.log.warning(
                    f"[initTP] soft TP cap reached active={self._active_tp_orders}/{self._max_tp_orders}; "
                    f"creating one safety {side} TP for missing={missing} hard={self._max_tp_orders_hard}"
                )
        chunks = 1 if force_compact_safety else min(3, slots)
        existing_prices = [
            safe_float(tp)
            for lid, tp in self._layer_tps.items()
            if self._tp_lid_side(lid) == side
        ]
        created = 0
        remaining = missing
        for idx in range(chunks):
            qty_part = round((remaining / (chunks - idx)) / self._qty_step) * self._qty_step
            qty_part = min(remaining, max(self._min_qty, qty_part))
            if qty_part < self._min_qty:
                continue
            mult = 1.0 + 0.5 * idx
            if side == "long":
                tp_price = pos.entry * (1 + self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * mult, "maker"))
            else:
                tp_price = pos.entry * (1 - self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * mult, "maker"))
            if any(abs(tp_price - px) / max(abs(px), 0.0001) < self._effective_side_step(side) * 0.25 for px in existing_prices):
                adjusted_mult = mult + 0.25
                if side == "long":
                    tp_price = pos.entry * (1 + self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * adjusted_mult, "maker"))
                else:
                    tp_price = pos.entry * (1 - self._effective_tp_ratio(self._effective_side_tp_ratio(side, "first") * adjusted_mult, "maker"))
            # Distinct model prices can collapse to the same exchange tick.
            # Avoid creating exact duplicate TP orders that the deduplicator
            # immediately cancels, leaving the position uncovered again.
            tp_price = self._round_tp_price_away(side, tp_price)
            tick = max(safe_float(getattr(self, "_tick_size", 0.0)), 1e-12)
            direction = 1.0 if side == "long" else -1.0
            for _ in range(32):
                if all(abs(tp_price - px) > tick * 0.5 for px in existing_prices):
                    break
                tp_price = round(tp_price + direction * tick, 12)
            lid = str(self._next_layer_id * 2 if side == "long" else self._next_layer_id * 2 + 1)
            self._next_layer_id += 1
            self._layer_tps[lid] = tp_price
            self._layer_qty_map[lid] = qty_part
            self._layer_side_map[lid] = side
            before = len(self._layer_tp_orders)
            self._place_tp_order(lid, side, qty_part, tp_price)
            if len(self._layer_tp_orders) > before:
                self._active_tp_orders += 1
                created += 1
                remaining = max(0.0, remaining - qty_part)
                existing_prices.append(tp_price)
            else:
                self._layer_tps.pop(lid, None)
                self._layer_qty_map.pop(lid, None)
                self._layer_side_map.pop(lid, None)
                break
        if created:
            self.log.info(f"[initTP] supplemented {side} TP coverage missing={missing} created={created}")
        return created

    def _tp_compaction_reason(self, live_orders) -> str:
        tp_orders = [
            order for order in live_orders
            if str(order.get("reduceOnly", "")).lower() == "true"
        ]
        total = len(tp_orders)
        if total > self._max_tp_orders:
            return f"soft_cap_{total}_{self._max_tp_orders}"
        side_counts = {"long": 0, "short": 0}
        price_groups = {}
        for order in tp_orders:
            side = str(order.get("posSide", "")).lower()
            if side in side_counts:
                side_counts[side] += 1
            key = (side, round(safe_float(order.get("px", 0)), 12))
            price_groups[key] = price_groups.get(key, 0) + 1
        duplicate_count = max(price_groups.values(), default=0)
        if total >= min(10, self._max_tp_orders):
            reason = f"count_{total}"
        elif max(side_counts.values(), default=0) > 6:
            reason = f"side_{max(side_counts.values())}"
        elif duplicate_count >= 3:
            reason = f"duplicate_{duplicate_count}"
        else:
            self._tp_compact_candidate_signature = None
            self._tp_compact_candidate_since = 0.0
            return ""
        now = time.time()
        recent_fill_ms = int(getattr(self, "_fill_sync_ts_ms", 0) or 0)
        if (
            getattr(self, "_trading_halted", False)
            or self._in_startup_recovery()
            or self._in_post_recovery_refill_delay()
            or now - getattr(self, "_last_position_change_ts", 0.0) < 15.0
            or (recent_fill_ms > 0 and 0 <= now * 1000 - recent_fill_ms < 15000)
            or now - getattr(self, "_last_tp_compact_ts", 0.0) < 300.0
        ):
            self._tp_compact_candidate_signature = None
            self._tp_compact_candidate_since = 0.0
            return ""
        signature = tuple(sorted(
            (
                str(order.get("ordId", "")),
                str(order.get("posSide", "")),
                str(order.get("px", "")),
                str(order.get("sz", "")),
            )
            for order in tp_orders
        ))
        if signature != getattr(self, "_tp_compact_candidate_signature", None):
            self._tp_compact_candidate_signature = signature
            self._tp_compact_candidate_since = now
            self.log.info(f"[initTP] compaction candidate {reason}; wait for stable snapshot")
            return ""
        if now - getattr(self, "_tp_compact_candidate_since", 0.0) < 30.0:
            return ""
        return reason

    def _init_existing_tp(self, cancel_existing=True):
        if self._tp_init_done and cancel_existing:
            return
        live_orders = []
        if self._client:
            try:
                live_orders = self._fetch_live_orders()
                live_orders = self._deduplicate_live_tp_orders(live_orders)
                if cancel_existing and live_orders:
                    cnt = 0
                    for o in live_orders:
                        if o.get("reduceOnly") != "true":
                            continue
                        oid = o.get("ordId", "")
                        if oid:
                            try:
                                self._cancel_order(oid)
                                cnt += 1
                            except Exception as e:
                                self.log.warning(f"[initTP] 撤销陈旧订单失败: {e}")
                    if cnt:
                        self.log.info(f"[initTP] cancelled {cnt} stale orders from exchange")
            except Exception as e:
                self.log.warning(f"[initTP] 读取挂单失败: {e}")
                self._tp_init_done = False
                self._tp_compaction_in_progress = False
                return
        # A position can reach zero before the exchange removes its live TP
        # snapshot. Cancel and exclude those stale reduce-only orders before
        # restoring local TP state, otherwise the next refresh can resubmit
        # them and produce OKX 51169.
        flat_sides = {
            side
            for side, position in (("long", self._long), ("short", self._short))
            if position.qty <= self._position_qty_tolerance()
        }
        if flat_sides and live_orders:
            retained_orders = []
            for order in live_orders:
                pside = str(order.get("posSide") or "").lower()
                is_tp = str(order.get("reduceOnly", "")).lower() == "true"
                oid = str(order.get("ordId") or "")
                if is_tp and pside in flat_sides:
                    if oid:
                        try:
                            self._cancel_order(oid)
                        except Exception as exc:
                            self.log.warning(f"[initTP] flat-side TP cancel failed oid={oid}: {exc}")
                    continue
                retained_orders.append(order)
            live_orders = retained_orders
            for side in flat_sides:
                self._invalidate_tp_side(side, reason="exchange_flat", cancel_orders=False)
        if not cancel_existing and getattr(self, "_tp_compaction_pending", None):
            live_orders = self._finish_tp_compaction(self._tp_compaction_pending)
            if live_orders is None:
                self._tp_init_done = False
                return
        compaction_reason = self._tp_compaction_reason(live_orders) if not cancel_existing else ""
        if compaction_reason:
            coverage = self._tp_coverage_from_orders(live_orders)
            tolerance = self._position_qty_tolerance()
            uncovered_sides = [
                side
                for side, position in (("long", self._long), ("short", self._short))
                if position.qty > 0 and coverage[side] + tolerance < position.qty
            ]
            if uncovered_sides:
                self.log.warning(
                    f"[TP compact] defer until coverage restored sides={','.join(uncovered_sides)}"
                )
            else:
                self._tp_init_done = False
                compacted_orders = self._compact_fragmented_tp_orders(live_orders, compaction_reason)
                if compacted_orders is None:
                    return
                live_orders = compacted_orders
        live_tp_count = sum(str(order.get("reduceOnly", "")).lower() == "true" for order in live_orders)
        if cancel_existing and live_tp_count:
            try:
                live_orders = self._fetch_live_orders()
            except Exception as e:
                self._tp_init_done = False
                self._tp_compaction_in_progress = False
                self.log.error(f"[initTP] 撤单后复核失败，暂不重建TP: {e}")
                return
        restored_tp_signature_before = getattr(self, "_last_restored_tp_signature", None)
        restored = self._restore_live_tp_orders(live_orders)
        restored_tp_changed = (
            getattr(self, "_last_restored_tp_signature", None) != restored_tp_signature_before
        )
        if not self._client or (self._long.qty <= 0 and self._short.qty <= 0):
            return
        tiers_short = [(0.5, 1.0), (0.3, 1.5), (0.2, 2.0)]
        tiers_long = [(0.5, 1.0), (0.5, 1.5)]
        tp_created = 0
        if restored["long"] == 0:
            tp_created += self._seed_layered_tp_side("long", self._long, tiers_long)
        if restored["short"] == 0:
            tp_created += self._seed_layered_tp_side("short", self._short, tiers_short)
        tp_created += self._supplement_tp_coverage_side("long", self._long)
        tp_created += self._supplement_tp_coverage_side("short", self._short)
        self._active_tp_orders = len(self._layer_tp_orders)
        total_tp = restored["long"] + restored["short"] + tp_created
        if total_tp:
            # Initialization is complete even when no TP could be placed.  A
            # zero-order result must become a hard opening block, otherwise a
            # failed/uneconomic TP placement would silently leave the position
            # uncovered while the grid continues to add inventory.
            self._tp_init_done = True
            should_log_tp_summary = bool(tp_created or restored_tp_changed)
            if should_log_tp_summary and tp_created and (restored["long"] or restored["short"]):
                self.log.info(
                    f"[initTP] restored {restored['long'] + restored['short']} live TP orders, created {tp_created} missing layered TP orders"
                )
            elif should_log_tp_summary and (restored["long"] or restored["short"]):
                self.log.info(f"[initTP] restored {restored['long'] + restored['short']} live TP orders")
            elif should_log_tp_summary:
                self.log.info(f"[initTP] created {tp_created} layered TP orders")
        else:
            # The exchange snapshot was valid and the strategy has positions,
            # but every TP placement failed or was rejected as uneconomic.
            # Keep the initialized state so _opening_order_block_reason()
            # returns tp_coverage_incomplete for subsequent additions.
            self._tp_init_done = True

    def _record_tp_transaction(self, side: str, entry: float, exit_px: float, qty: float, gross: float, fee: float, net: float, ts_ms: int = 0):
        try:
            csv_path = self._pnl_csv_path
            header = "time,symbol,side,qty,entry,exit,gross,fee,net\n"
            if ts_ms > 0:
                ts_text = datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')
            else:
                ts_text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            line = f"{ts_text},{self._symbol},{side},{qty:.4f},{entry:.4f},{exit_px:.4f},{gross:.4f},{fee:.4f},{net:.4f}\n"
            os.makedirs("data", exist_ok=True)
            needs_header = (not os.path.exists(csv_path)) or os.path.getsize(csv_path) == 0
            with open(csv_path, "a") as f:
                if needs_header:
                    f.write(header)
                f.write(line)
        except Exception as e:
            self.log.warning(f"[PnL] 写入交易记录失败: {e}")

    def _check_opbot_tp(self, price: float) -> Optional[dict]:
        """Opbot单侧止盈: avg_entry +/- tp_ratio -> close_side"""
        # Live positions are protected by exchange-hosted reduce-only TP orders.
        # A strategy-side market close can partially fill and leave the remainder
        # uncovered, so the legacy Opbot close path is simulation-only.
        if self._client:
            return None
        if self._long.qty > 0 and self._long.entry > 0:
            tp_target = self._long.entry * (1 + self._effective_tp_ratio(self._base_tp_ratio, "maker"))
            if price >= tp_target:
                self.log.info(f"[OpbotTP] 多头止盈 @ {price:.4f} >= {tp_target:.4f}")
                return self._close_side("long")
        if self._short.qty > 0 and self._short.entry > 0:
            tp_target = self._short.entry * (1 - self._effective_tp_ratio(self._base_tp_ratio, "maker"))
            if price <= tp_target:
                self.log.info(f"[OpbotTP] 空头止盈 @ {price:.4f} <= {tp_target:.4f}")
                return self._close_side("short")
        return None

    def _cleanup_stale_tps(self, price):
        for lid, tp in list(self._layer_tps.items()):
            side = self._tp_lid_side(lid)
            if float(tp) <= 0:
                continue
            position = self._long if side == "long" else self._short
            invalid = position.qty <= 0 or (
                position.entry > 0
                and (
                    (side == "long" and float(tp) <= position.entry)
                    or (side == "short" and float(tp) >= position.entry)
                )
            )
            if not invalid:
                continue
            self._cancel_tp_order(lid)
            self._layer_tps.pop(lid, None)
            self._layer_qty_map.pop(lid, None)
            self._layer_side_map.pop(lid, None)

    def _heal_missing_tp_coverage(self) -> None:
        """Runtime self-heal for TP coverage that dropped to zero.

        Legacy gaps: a side whose TP orders vanish at runtime (cancel, fill, exchange
        glitch, or the stale-exchange-coverage cache fooled ``_supplement_tp_coverage_side``
        into thinking it was already covered) could only be rebuilt at startup
        (``_init_existing_tp``) or inside the sync-recovery branch, so it stayed
        permanently uncovered and its adds were blocked by ``tp_coverage_incomplete``
        until the next restart.

        This hook runs on its own timer inside the main cycle. It pulls the exchange's
        live reduce-only TP orders to refresh the ground-truth coverage cache, then for
        any side holding inventory with ~no live TP it calls ``_supplement_tp_coverage_side``
        to rebuild that side only. Because the decision is based on exchange live orders
        (not the local ``_layer_qty_map``), it also correctly detects phantom-coverage
        cases where local reservations were dropped mid-flight.
        """
        if not self._client:
            return
        now = time.time()
        interval = max(30.0, safe_float(getattr(self, "_tp_heal_interval", 120.0)))
        if now - getattr(self, "_last_tp_heal", 0.0) < interval:
            return
        self._last_tp_heal = now
        if getattr(self, "_in_startup_recovery", lambda: False)():
            return
        try:
            live = self._deduplicate_live_tp_orders(self._fetch_live_orders())
        except Exception as exc:
            self.log.warning(f"[TP-heal] live order fetch failed: {exc}")
            return
        # Refresh the ground-truth coverage cache so the supplement gap is real.
        self._update_exchange_tp_coverage(live)
        for side in ("long", "short"):
            pos = self._long if side == "long" else self._short
            if safe_float(getattr(pos, "qty", 0.0)) <= 0 or safe_float(getattr(pos, "entry", 0.0)) <= 0:
                continue
            covered = self._risk_tp_covered_qty(side)
            if covered >= pos.qty - self._position_qty_tolerance():
                continue
            gap = pos.qty - covered
            if gap <= self._tp_coverage_tolerance(side, pos.qty):
                continue
            self.log.warning(
                f"[TP-heal] {self._symbol} {side} missing live TP coverage "
                f"covered={covered:.4f} pos={pos.qty:.4f} gap={gap:.4f}; rebuilding"
            )
            self._supplement_tp_coverage_side(side, pos)

    def _rehang_far_tps(self, price: float):
        """Pull layer TPs stranded far from market back toward cost.

        Three rails so a rehang can never manufacture an instant fill or a
        net-exposure spike:
          * only when the current TP sits at least ``min_gap`` away from market;
          * the new price keeps a 20bps distance from market (no instant fill);
          * projected net notional after this layer's TP must stay inside
            ``net_headroom`` of the hard net cap.
        """
        if not bool(getattr(self, "_tp_rehang_enabled", False)):
            return
        if price <= 0 or not self._layer_tps:
            return
        dry = bool(getattr(self, "_tp_rehang_dry_run", True))
        min_gap = max(0.002, safe_float(getattr(self, "_tp_rehang_min_gap_ratio", 0.02)))
        step = min(1.0, max(0.05, safe_float(getattr(self, "_tp_rehang_step_ratio", 0.5))))
        cooldown = max(60.0, safe_float(getattr(self, "_tp_rehang_cooldown_sec", 900.0)))
        max_times = max(1, int(safe_float(getattr(self, "_tp_rehang_max_times", 4))))
        headroom = min(1.0, max(0.1, safe_float(getattr(self, "_tp_rehang_net_headroom", 0.8))))
        net_hard = max(0.0, safe_float(getattr(self, "_max_net_notional_usdt", 0.0)))
        if net_hard <= 0:
            # Fall back to the soft cap, then refuse to rehang: an unset limit
            # must never turn the net-exposure rail into a no-op.
            net_hard = max(0.0, safe_float(getattr(self, "_max_net_notional_soft_usdt", 0.0)))
        if net_hard <= 0:
            return
        ct_val = max(1e-12, safe_float(getattr(self, "_ct_val", 1.0)))
        edge = 0.002
        now = time.time()
        ttl = max(0.0, safe_float(getattr(self, "_tp_rehang_state_ttl_sec", 0.0)))
        dirty = False
        state = getattr(self, "_tp_rehang_state", None)
        if not isinstance(state, dict):
            state = {}
            self._tp_rehang_state = state
        for lid, tp_raw in list(self._layer_tps.items()):
            side = self._tp_lid_side(lid)
            if side not in {"long", "short"}:
                continue
            tp = safe_float(tp_raw, 0.0)
            if tp <= 0:
                continue
            pos = self._long if side == "long" else self._short
            if pos.qty <= 0 or pos.entry <= 0:
                continue
            _rec = state.get(lid)
            if isinstance(_rec, dict) and ttl > 0:
                _ts = safe_float(_rec.get("ts", 0.0))
                if _ts > 0 and now - _ts > ttl:
                    state.pop(lid, None)
                    dirty = True
            gap = (tp / price - 1.0) if side == "long" else (price / tp - 1.0)
            if gap < min_gap:
                continue
            layer_qty = safe_float(self._layer_qty_map.get(lid, 0.0), 0.0)
            if layer_qty <= 0:
                layer_qty = pos.qty * 0.5
            layer_qty = min(layer_qty, pos.qty)
            if layer_qty <= 0:
                continue
            target = self._minimum_profitable_tp_price(side, layer_qty, pos.entry)
            if target <= 0:
                continue
            if side == "long":
                target = max(target, price * (1.0 + edge))
                if target >= tp:
                    continue
                new_tp = tp + (target - tp) * step
                new_tp = max(new_tp, target)
                if new_tp <= price * (1.0 + edge):
                    new_tp = price * (1.0 + 2.0 * edge)
            else:
                target = min(target, price * (1.0 - edge))
                if target <= tp:
                    continue
                new_tp = tp + (target - tp) * step
                new_tp = min(new_tp, target)
                if new_tp >= price * (1.0 - edge):
                    new_tp = price * (1.0 - 2.0 * edge)
            if net_hard > 0:
                new_long = self._long.qty - (layer_qty if side == "long" else 0.0)
                new_short = self._short.qty - (layer_qty if side == "short" else 0.0)
                net_after = (new_long - new_short) * ct_val * price
                if abs(net_after) > net_hard * headroom:
                    rec = state.get(lid) or {}
                    if now - safe_float(rec.get("skip_ts", 0.0)) >= cooldown:
                        self.log.info(
                            f"[TP-rehang] skip {lid} {side}: net_after={net_after:.1f}U "
                            f"> {net_hard * headroom:.1f}U limit"
                        )
                        rec["skip_ts"] = now
                        state[lid] = rec
                        dirty = True
                    continue
            rec = state.get(lid) or {}
            if int(safe_float(rec.get("n", 0))) >= max_times:
                continue
            if now - safe_float(rec.get("ts", 0.0)) < cooldown:
                continue
            new_tp = self._round_tp_price_away(side, new_tp)
            if new_tp <= 0 or abs(new_tp - tp) / tp < 1e-4:
                continue
            msg = (
                f"[TP-rehang] {lid} {side} qty={layer_qty:.4f} "
                f"{tp:.6g} -> {new_tp:.6g} (gap={gap * 100:.1f}%)"
            )
            if dry:
                self.log.info(msg + " [dry-run]")
                continue
            self._cancel_tp_order(lid)
            self._layer_tps.pop(lid, None)
            self._layer_qty_map.pop(lid, None)
            self._layer_side_map.pop(lid, None)
            ok = self._place_tp_order(lid, side, layer_qty, new_tp, exclude_qty=layer_qty)
            state[lid] = {
                "n": int(safe_float(rec.get("n", 0))) + 1,
                "ts": now,
                "last": new_tp if ok else tp,
            }
            dirty = True
            if ok:
                self._layer_tps[lid] = new_tp
                self._layer_qty_map[lid] = layer_qty
                self._layer_side_map[lid] = side
                self.log.info(msg + " ok")
            else:
                failure = repr(getattr(self, "_last_tp_place_failure", {}))[:200]
                self.log.warning(msg + " place_failed " + failure)
        if dirty:
            persist = getattr(self, "_save_pnl_data", None)
            if callable(persist):
                try:
                    persist()
                except Exception as e:
                    self.log.warning(f"[TP-rehang] state save failed: {e}")

    def _invalidate_tp_side(self, side: str, reason: str = "", cancel_orders: bool = True):
        """Drop local TP state after the exchange says a side is flat."""
        side = str(side or "").lower()
        if side not in {"long", "short"}:
            return
        lids = set(self._layer_tps) | set(self._layer_qty_map) | set(self._layer_side_map)
        lids |= set(self._layer_tp_orders)
        for lid in list(lids):
            if self._tp_lid_side(lid) != side:
                continue
            if cancel_orders:
                self._cancel_tp_order(lid)
            else:
                self._layer_tp_orders.pop(lid, None)
            self._layer_tps.pop(lid, None)
            self._layer_qty_map.pop(lid, None)
            self._layer_side_map.pop(lid, None)
            self._tp_rehang_state.pop(lid, None)
        self._active_tp_orders = len(self._layer_tp_orders)
        self._tp_no_position_until[side] = max(
            safe_float(self._tp_no_position_until.get(side, 0.0)),
            time.time() + 15.0,
        )
        if side == "long":
            self._long = Pos("long")
            self._long_layer = 0
            self._long_add_trigger_price = 0.0
        else:
            self._short = Pos("short")
            self._short_layer = 0
            self._short_add_trigger_price = 0.0
        if not hasattr(self, "_normal_close_sides"):
            self._normal_close_sides = {"long": False, "short": False}
        self._normal_close_sides[side] = True
        self.log.info(f"[TP] invalidate side={side} reason={reason or 'exchange_flat'}")

    # ==================== 分层止盈(修复重复代码) ====================

    def _place_tp_order(self, lid: str, side: str, qty: float, tp_price: float, coverage_safety: bool = False, exclude_qty: float = 0.0):
        self._last_tp_place_failure = {"reason": "unknown"}
        if not self._client or qty <= 0 or tp_price <= 0:
            self._last_tp_place_failure = {"reason": "invalid_request"}
            return False
        side = str(side or "").lower()
        if side not in {"long", "short"}:
            self._last_tp_place_failure = {"reason": "invalid_side"}
            return False
        if time.time() < safe_float(self._tp_no_position_until.get(side, 0.0)):
            self._last_tp_place_failure = {"reason": "no_position_backoff"}
            return False
        snapshot = self._exchange_position_snapshot(side)
        if snapshot is None:
            self._last_tp_place_failure = {"reason": "position_query_failed"}
            return False
        live_qty, live_entry = snapshot
        tolerance = self._position_qty_tolerance()
        if live_qty <= tolerance:
            if not self._exchange_side_flat_confirmed(side):
                self._last_tp_place_failure = {"reason": "exchange_flat_unconfirmed", "live_qty": live_qty}
                return False
            self._invalidate_tp_side(side, reason="exchange_flat")
            self._last_tp_place_failure = {"reason": "exchange_flat", "live_qty": live_qty}
            return False
        pos = self._long if side == "long" else self._short
        if live_entry > 0 and pos.entry <= 0:
            pos.entry = live_entry
        # Do not let a stale local layer map submit more TP than the live
        # position. Exclude this lid because its requested qty is not live yet.
        #
        # A layer entry with no exchange order id never reached the book: it is
        # either a reservation dropped mid-flight or a cancelled/filled order
        # whose local mapping survived. Counting those phantom layers as real
        # coverage drives available_qty to zero, so every replacement TP fails
        # with qty_below_min. Coverage then stays empty forever and
        # tp_coverage_incomplete blocks every add on that side. Only layers
        # backed by a live order id may count as coverage.
        covered_other = self._tp_covered_other_qty(
            side, lid, live_qty, tolerance, exclude_qty=max(0.0, abs(safe_float(exclude_qty))),
        )
        available_qty = max(0.0, live_qty - covered_other)
        qty = min(abs(safe_float(qty)), available_qty)
        step = max(0.0, safe_float(getattr(self, "_qty_step", 0.0)))
        if step > 0:
            qty = math.floor((qty + 1e-12) / step) * step
        if qty + tolerance < max(safe_float(getattr(self, "_min_qty", 0.0)), tolerance):
            self._last_tp_place_failure = {
                "reason": "qty_below_min",
                "live_qty": live_qty,
                "available_qty": available_qty,
                "qty": qty,
            }
            return False
        minimum_price = self._minimum_profitable_tp_price(side, qty, pos.entry)
        if coverage_safety:
            if not self._coverage_safety_tp_allowed(side, qty, tp_price, pos):
                self._last_tp_place_failure = {"reason": "coverage_safety_limit"}
                return False
        elif side == "long":
            tp_price = max(tp_price, minimum_price)
        else:
            tp_price = min(tp_price, minimum_price)
        # OKX rejects limit prices outside the instrument price band (51006):
        # a TP derived from a stale entry can fall below the current min sell
        # price. When the position is already in profit, lifting the TP to
        # just beyond the market is both legal and profitable; when it would
        # close at a loss we leave the original price so the dangerous-TP
        # guard below still rejects it.
        if bool(getattr(self, "_tp_price_band_enabled", True)) and pos.entry > 0:
            mark_ref = safe_float(getattr(self, "_last_price", 0.0))
            band_gap = safe_float(getattr(self, "_tp_price_band_gap_ratio", 0.003))
            if mark_ref > 0 and band_gap > 0:
                if side == "long" and tp_price < mark_ref * (1.0 + band_gap):
                    candidate = mark_ref * (1.0 + band_gap)
                    if candidate > pos.entry:
                        tp_price = candidate
                elif side == "short" and tp_price > mark_ref * (1.0 - band_gap):
                    candidate = mark_ref * (1.0 - band_gap)
                    if candidate < pos.entry:
                        tp_price = candidate
        tp_price = self._round_tp_price_away(side, tp_price)
        self._layer_tps[lid] = tp_price
        if pos.entry > 0 and ((side == "long" and tp_price <= pos.entry) or (side == "short" and tp_price >= pos.entry)):
            self.log.warning(f"[TP限价单] 拒绝危险TP side={side} entry={pos.entry:.4f} tp={tp_price:.4f} qty={qty}")
            self._layer_tps.pop(lid, None)
            self._layer_qty_map.pop(lid, None)
            self._layer_side_map.pop(lid, None)
            self._last_tp_place_failure = {
                "reason": "dangerous_tp",
                "live_qty": live_qty,
                "entry": pos.entry,
                "tp_price": tp_price,
            }
            return False
        try:
            tp_side = "sell" if side == "long" else "buy"
            r = self._submit_order(self._symbol, tp_side, str(qty),
                ord_type="limit", px=str(tp_price), pos_side=side, reduce_only=True)
            if r:
                order_id = r.get("ordId", "")
                if not order_id:
                    self._last_tp_place_failure = {
                        "reason": "api_missing_order_id",
                        "code": self._order_error_code(r),
                        "result": repr(r)[:300],
                    }
                    return False
                self._layer_tp_orders[lid] = order_id
                self.log.info(f"[TP限价单] {side} {qty}张 @ {tp_price} oid={r.get('ordId','?')}")
                return True
            self._last_tp_place_failure = {
                "reason": "submit_rejected",
                "code": self._order_error_code(r),
                "result": repr(r)[:300],
            }
        except Exception as e:
            self._last_tp_place_failure = {"reason": "api_exception", "error": repr(e)[:300]}
            self.log.error(f"[TP限价单] 下单失败: {e}")
        return False

    def _should_keep_hanging_tp(self, lid: str, price: float) -> bool:
        side = self._tp_lid_side(lid)
        tp = safe_float(self._layer_tps.get(lid, 0.0))
        if tp <= 0 or price <= 0 or side not in {"long", "short"}:
            return False
        near_band = max(self._base_tp_ratio * 0.35, self._add_step * 0.8, 0.0015)
        if side == "long":
            return price >= tp * (1.0 - near_band)
        return price <= tp * (1.0 + near_band)

    def _cancel_tp_order(self, lid: str):
        if lid in self._layer_tp_orders and self._client:
            oid = self._layer_tp_orders.pop(lid, "")
            if oid:
                try:
                    self._cancel_order(oid)
                except Exception as e:
                    self.log.warning(f"[TP] 撤单失败 lid={lid} oid={oid}: {e}")

    def _cancel_all_tp_orders(self):
        for lid in list(self._layer_tp_orders.keys()):
            self._cancel_tp_order(lid)

    def _check_layered_tp(self, price: float) -> Optional[dict]:
        if self._active_tp_orders >= self._max_tp_orders:
            return None
        for lid, tp in list(self._layer_tps.items()):
            side = self._tp_lid_side(lid)
            hit = (price >= tp and side == "long") or (price <= tp and side == "short")
            if not hit:
                continue
            pos = self._long if side == "long" else self._short
            if pos.qty <= 0:
                continue
            if (side == "long" and float(tp) <= pos.entry) or (side == "short" and float(tp) >= pos.entry):
                self.log.warning(f"[分层止盈] 跳过危险TP lid={lid} side={side} entry={pos.entry:.4f} tp={float(tp):.4f}")
                self._cancel_tp_order(lid)
                self._layer_tps.pop(lid, None)
                self._layer_qty_map.pop(lid, None)
                self._layer_side_map.pop(lid, None)
                continue
            self._cancel_tp_order(lid)
            self._layer_tps.pop(lid, None)
            self._layer_side_map.pop(lid, None)
            self._active_tp_orders = max(0, self._active_tp_orders - 1)
            # Use exact layer qty from map, fall back to 50% if missing
            close_qty = self._layer_qty_map.pop(lid, pos.qty * 0.5)
            close_qty = min(close_qty, pos.qty)  # never close more than we have
            pos.qty -= close_qty
# balance code removed - was closing opposite side incorrectly
            bal_qty = 0.0

            contract_value = max(self._ct_val, 1e-12)
            gross = contract_value * (close_qty * (price - pos.entry) if side == "long" else close_qty * (pos.entry - price))
            fee = 0.0; net = 0.0
            if self._client:
                fee = close_qty * contract_value * price * self.MAKER_FEE
                net = gross - fee
                self._total_fees += fee; self._total_pnl += net
            self._save_pnl_data()
            order = "sell" if side == "long" else "buy"
            self.log.info(f"[分层止盈] {lid} {side}: gross={gross:.2f}U fee={fee:.4f}U net={net:.2f}U" + (" (模拟)" if not self._client else ""))
            return {"type":"layer_tp","action":order,"quantity":str(close_qty),"pos_side":side,
                    "pnl":round(net,4),"sCode":"0","order_type":"limit","price":str(tp)}
        return None

    # ==================== 成交检验(移除不存在的_place_next_layer) ====================
