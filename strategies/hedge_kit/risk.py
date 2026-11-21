"""风控、熔断与暂停判定。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class RiskMixin:
    """风控、熔断与暂停判定（19 个方法）。"""

    def _load_halt_state(self):
        try:
            if not os.path.exists(self._halt_state_file):
                return
            with open(self._halt_state_file, "r") as f:
                d = json.load(f)
            if not d.get("halted", False):
                return
            self._trading_halted = True
            self._halt_reason = d.get("reason", "persisted_halt")
            self._resume_required = bool(d.get("resume_required", True))
            self._halt_operator = str(d.get("operator", "") or "")
            self._halt_updated_at = d.get("updated_at", "")
            self.log.warning(f"[halt] restored persisted halt state: {self._halt_reason}")
        except Exception as e:
            self.log.warning(f"[halt] load state failed: {e}")

    def _save_halt_state(self):
        try:
            payload = {
                "halted": self._trading_halted,
                "reason": self._halt_reason,
                "symbol": self._symbol,
                "resume_required": bool(getattr(self, "_resume_required", False)),
                "operator": str(getattr(self, "_halt_operator", "") or ""),
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            os.makedirs("data", exist_ok=True)
            with open(self._halt_state_file, "w") as f:
                json.dump(payload, f, indent=2)
            self._halt_updated_at = payload["updated_at"]
        except Exception as e:
            self.log.warning(f"[halt] save state failed: {e}")

    def _clear_halt(self, note: str = ""):
        if self._trading_halted:
            self.log.warning(f"[halt] clearing halt state: {note or self._halt_reason}")
        self._trading_halted = False
        self._halt_reason = ""
        self._resume_required = False
        self._halt_operator = str(note or "")
        self._save_halt_state()

    def _ensure_daily_risk_window(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl_date = today
            self._day_start_total_pnl = self._total_pnl
            self._day_equity_baseline = self._cached_equity if self._cached_equity > 0 else self._risk_equity_baseline
            if not bool(getattr(self, "_daily_stop_latched", False)):
                self._daily_stop_until = 0.0
                self._daily_stop_reason = ""
            self._save_pnl_data()
        elif self._day_equity_baseline <= 0 and self._cached_equity > 0:
            self._day_equity_baseline = self._cached_equity
            self._save_pnl_data()

    def _daily_drawdown_ratio(self) -> float:
        from strategies.hedge_calculations import daily_drawdown_ratio
        return daily_drawdown_ratio(self._day_equity_baseline, self._cached_equity)

    def _risk_opening_pause_active(self) -> bool:
        until = safe_float(getattr(self, "_risk_opening_pause_until", 0.0))
        if until > time.time():
            return True
        if until > 0:
            self._risk_opening_pause_until = 0.0
            self._risk_opening_pause_reason = ""
        return False

    def pause_opening(self, reason: str, seconds: float, cancel_existing: bool = True):
        was_active = self._risk_opening_pause_active()
        self._risk_opening_pause_until = max(
            safe_float(getattr(self, "_risk_opening_pause_until", 0.0)),
            time.time() + max(3.0, safe_float(seconds)),
        )
        self._risk_opening_pause_reason = str(reason or "risk_opening_pause")
        if cancel_existing and not was_active:
            self._cancel_opening_orders()
        self._set_add_block(reason=self._risk_opening_pause_reason)

    def opening_pause_status(self) -> Dict:
        active = self._risk_opening_pause_active()
        return {
            "active": active,
            "reason": self._risk_opening_pause_reason if active else "",
            "remaining_sec": max(
                0.0, safe_float(getattr(self, "_risk_opening_pause_until", 0.0)) - time.time()
            ) if active else 0.0,
        }

    def _log_opening_guard_block(self, side: str, reason: str, qty: float, price: float):
        """Log deterministic opening blocks once per side/reason window."""
        reason = str(reason or "opening_guard")
        side = str(side or "")
        now = time.time()
        events = [
            item for item in getattr(self, "_opening_guard_events", [])
            if now - safe_float(item.get("at")) <= safe_float(getattr(self, "_opening_guard_observe_window_sec", 900.0))
        ]
        events.append({"at": now, "side": side, "reason": reason})
        self._opening_guard_events = events
        state = getattr(self, "_opening_guard_log_state", {})
        last_reason, last_ts = state.get(side, ("", 0.0))
        interval = 300.0 if reason in {"max_side_notional", "max_total_notional", "max_net_notional"} else 60.0
        if reason == last_reason and now - last_ts < interval:
            return
        self.log.warning(
            f"[opening_guard] blocked {self._symbol} {side} qty={qty} px={price:.4f} reason={reason}"
        )
        state[side] = (reason, now)
        self._opening_guard_log_state = state
        ledger = getattr(getattr(self, "_client", None), "_event_ledger", None)
        if ledger is not None:
            try:
                order_side = "buy" if side == "long" else "sell" if side == "short" else ""
                ledger.record_local_rejection(
                    symbol=self._symbol,
                    side=order_side,
                    pos_side=side,
                    quantity=qty,
                    price=price,
                    reason_code=reason,
                    payload={"strategy": self._profile_slug},
                )
            except Exception as exc:
                self.log.warning(f"[opening_guard] audit write failed: {exc}")

    def _opening_guard_stats(self) -> dict:
        now = time.time()
        window = max(60.0, safe_float(getattr(self, "_opening_guard_observe_window_sec", 900.0)))
        events = [
            item for item in getattr(self, "_opening_guard_events", [])
            if now - safe_float(item.get("at")) <= window
        ]
        self._opening_guard_events = events
        by_reason = {}
        by_side = {}
        for item in events:
            reason = str(item.get("reason") or "unknown")
            side = str(item.get("side") or "")
            by_reason[reason] = by_reason.get(reason, 0) + 1
            if side:
                side_map = by_side.setdefault(side, {})
                side_map[reason] = side_map.get(reason, 0) + 1
        top_reason = max(by_reason, key=by_reason.get) if by_reason else ""
        return {
            "window_sec": window,
            "total": len(events),
            "by_reason": by_reason,
            "by_side": by_side,
            "top_reason": top_reason,
            "top_reason_count": by_reason.get(top_reason, 0),
            "updated_at": now,
        }

    def _risk_context_snapshot(self):
        base_margin_ratio = safe_float(getattr(self, "_max_margin_ratio", 0.0))
        # Keep account thresholds separate from the shared incremental budget
        # target. The budget target is a reservation limit, not a hard risk line.
        account_margin_soft = safe_float(
            getattr(self, "_account_margin_soft_limit", base_margin_ratio)
        )
        account_margin_hard = safe_float(
            getattr(self, "_account_margin_hard_limit", base_margin_ratio)
        )
        account_margin_resume = safe_float(
            getattr(self, "_account_margin_resume_limit", account_margin_soft)
        )
        return {
            "context_version": 1,
            "generated_at": time.time(),
            "symbol": str(getattr(self, "_symbol", "") or "").upper(),
            "positions": {
                "long_qty": self._long.qty,
                "short_qty": self._short.qty,
                "max_net_exposure": 0.0,
                "max_net_notional_usdt": safe_float(getattr(self, "_max_net_notional_usdt", 0.0)),
            },
            "account": {
                "margin_load_ratio": self._account_margin_load_ratio(),
                "equity_usdt": safe_float(getattr(self, "_cached_equity", 0.0)),
                "min_open_equity_usdt": safe_float(getattr(self, "_min_open_equity_usdt", 0.0)),
                "frozen_margin_usdt": safe_float(getattr(self, "_cached_frozen", 0.0)),
                "max_margin_ratio": safe_float(getattr(self, "_max_margin_ratio", 0.0)),
                "account_margin_soft_limit": account_margin_soft,
                "account_margin_hard_limit": account_margin_hard,
                "account_margin_resume_limit": account_margin_resume,
                "account_margin_scope": str(
                    getattr(self, "_account_margin_scope", "shared_okx_account")
                    or "shared_okx_account"
                ),
                # Compatibility alias for non-strategy callers. It must use
                # the true hard line, never the budget allocator target.
                "hard_margin_ratio": account_margin_hard,
                "net_reducing_hard_margin_exempt_enabled": bool(
                    getattr(self, "_net_reducing_hard_margin_exempt_enabled", False)
                ),
                "paired_net_neutral_max_margin_ratio": safe_float(
                    getattr(self, "_paired_add_max_margin_ratio", 0.0)
                ),
                "mgn_ratio_open_limit": safe_float(getattr(self, "_mgn_ratio_open_limit", 5.0)),
                "inventory_repair_hard_override_enabled": bool(
                    getattr(self, "_inventory_repair_hard_override_enabled", False)
                ),
                "inventory_repair_hard_override_symbol": str(
                    getattr(self, "_inventory_repair_hard_override_symbol", "") or ""
                ).upper(),
                "inventory_repair_hard_override_margin_ratio": safe_float(
                    getattr(self, "_inventory_repair_hard_override_margin_ratio", 0.0)
                ),
                "inventory_repair_hard_override_mgn_ratio_min": safe_float(
                    getattr(self, "_inventory_repair_hard_override_mgn_ratio_min", 0.0)
                ),
                "inventory_repair_hard_override_max_step_notional_usdt": safe_float(
                    getattr(
                        self,
                        "_inventory_repair_hard_override_max_step_notional_usdt",
                        0.0,
                    )
                ),
                "risk_context_max_age_sec": safe_float(
                    getattr(self, "_account_risk_max_age_sec", 15.0)
                ),
                "account_budget_enabled": bool(getattr(self, "_account_budget_allocator", None)),
                "budget_remaining_margin": safe_float(self._account_budget_snapshot().get("remaining_margin")),
                "mark_price": safe_float(getattr(self, "_last_price", 0.0)),
                "contract_value": safe_float(getattr(self, "_ct_val", 1.0)),
                "leverage": max(1.0, safe_float(getattr(self, "_cur_lev", 1.0))),
                "margin_buffer_ratio": safe_float(getattr(self, "_margin_buffer_ratio", 0.10)),
            },
            "account_risk": dict(self._account_risk_snapshot),
        }

    def _baseline_stale_reason(self, total_eq: float) -> str:
        """判定权益基线是否陈旧。返回原因串，空串表示健康。

        两类历史事故场景：
        - 基线远低于权益 → dd 恒为负，熔断器永久静默失效
        - 基线远高于权益 → 假熔断锁死交易（DRAM 曾被锁 55 天）
        """
        baseline = self._risk_equity_baseline
        if baseline <= 0 or total_eq <= 0:
            return ""
        drift = abs(baseline - total_eq) / total_eq
        if drift > self._baseline_reanchor_drift:
            return f"drift={drift*100:.0f}%"
        ts = self._risk_equity_baseline_ts
        max_age = self._baseline_max_age_days
        if ts > 0 and max_age > 0:
            age_days = (time.time() - ts) / 86400.0
            if age_days > max_age:
                return f"age={age_days:.0f}d"
        return ""

    def _handle_stale_baseline(self, total_eq: float) -> None:
        """非停机状态下把偏离/过期的基线重新锚定到当前权益，并邮件通知。

        硬停机期间绝不重锚——否则等于抹掉真实回撤记录。
        """
        reason = self._baseline_stale_reason(total_eq)
        if not reason:
            return
        if self._is_hard_stop_active() or self._trading_halted:
            now = time.time()
            if now - self._baseline_stale_log_ts > 600:
                self._baseline_stale_log_ts = now
                self.log.warning(
                    f"[risk] 检测到可疑权益基线({reason})，但停机保护中不重锚: "
                    f"baseline={self._risk_equity_baseline:.2f} equity={total_eq:.2f}"
                )
            return
        old = self._risk_equity_baseline
        self._risk_equity_baseline = total_eq
        self._risk_equity_baseline_ts = time.time()
        self._save_pnl_data()
        self.log.warning(
            f"[risk] 陈旧权益基线已重新锚定({reason}): {old:.2f} -> {total_eq:.2f}"
        )
        notifier = getattr(self, "_notifier", None)
        if notifier:
            try:
                notifier.alert(
                    "权益基线重新锚定",
                    f"{self._symbol} 基线 {old:.2f} 偏离当前权益({reason})，已重锚为 {total_eq:.2f}。",
                    key=f"baseline_reanchor:{self._symbol}",
                )
            except Exception:
                pass

    def _is_hard_stop_active(self) -> bool:
        if not getattr(self, "_hard_stop_enabled", False):
            return False
        if self._session_realized_pnl() < -self._max_loss_usdt:
            return True
        if self._cached_equity > 0 and self._risk_equity_baseline > 0:
            dd = (self._risk_equity_baseline - self._cached_equity) / self._risk_equity_baseline
            if dd > self._max_drawdown:
                self._ensure_daily_risk_window()
                return True
        return False

    def _engage_halt(self, reason: str):
        if not self._trading_halted:
            self.log.warning(f"[停机保护] 触发交易停机: {reason}")
            notifier = getattr(self, "_notifier", None)
            if notifier:
                notifier.alert(
                    "策略停机保护",
                    f"{self._symbol} 已停止交易，原因: {reason}",
                    key=f"strategy_halt:{self._symbol}:{reason}",
                )
        self._trading_halted = True
        self._halt_reason = reason
        self._resume_required = True
        self._save_halt_state()

    def _hard_stop_loss(self, pnl_rate, price):
        if not getattr(self, "_hard_stop_enabled", False):
            return False
        session_pnl = self._session_realized_pnl()
        if session_pnl < -self._max_loss_usdt:
            self.log.warning(f"[STOP] session_pnl {session_pnl:.2f}U < -{self._max_loss_usdt}U"); return True
        if self._cached_equity > 0 and self._risk_equity_baseline > 0:
            dd = (self._risk_equity_baseline - self._cached_equity) / self._risk_equity_baseline
            if dd > self._max_drawdown:
                self.log.warning(f"[STOP] 权益回撤 {dd:.1%} > {self._max_drawdown:.0%}"); return True
        return False


    # ==================== 净敞口自动纠偏（防止单边重仓） ====================
    # ==================== 挂单同步(重启后自动找回) ====================

    def _daily_stop(self):
        self._ensure_daily_risk_window()
        now = time.time()
        if bool(getattr(self, "_daily_stop_latched", False)):
            return True
        if self._daily_stop_until > now:
            return True
        daily_pnl = self._daily_realized_pnl()
        daily_dd = self._daily_drawdown_ratio()
        reason = ""
        if daily_pnl < -self._daily_loss_limit_usdt:
            reason = f"daily_pnl {daily_pnl:.2f}U"
        elif daily_dd > self._daily_drawdown:
            reason = f"daily_dd {daily_dd:.1%}"
        if not reason:
            return False
        next_day = datetime.combine(datetime.now().date() + timedelta(days=1), datetime.min.time()).timestamp()
        self._daily_stop_until = next_day
        self._daily_stop_reason = reason
        self._daily_stop_latched = True
        self._engage_halt(f"daily_stop:{reason}")
        self._save_pnl_data()
        self.log.warning(f"[risk] daily stop triggered: {reason}")
        notifier = getattr(self, "_notifier", None)
        if notifier:
            notifier.alert(
                "单日亏损停机",
                f"{self._symbol} 已停止当日新开仓，原因: {reason}",
                key=f"daily_stop:{self._symbol}:{datetime.now().date().isoformat()}",
            )
        return True

    def _risk_check(self, side: str, add_usdt: float) -> tuple:
        """Unified risk check -> (valid: bool, reason: str)"""
        self._sync_balance()
        need = add_usdt or self._effective_base_pos_usdt(side)
        if not self._check_rate_limit(side, need): return False, "rate limit"
        if self._daily_stop(): return False, "daily stop"
        if not self._check_margin(side, add_usdt): return False, "margin"
        free_avail = self._available_after_reserve()
        if free_avail > 0 and need * 1.02 > free_avail:
            return False, f"insufficient_free_margin need={need*1.02:.1f} free={free_avail:.1f} reserve={self._reserved_usdt:.1f}"
        # Per-side layer limit
        if side == "long" and self._long_layer >= self._effective_side_max_adds("long"):
            return False, "long max layers"
        if side == "short" and self._short_layer >= self._effective_side_max_adds("short"):
            return False, "short max layers"
        return True, ""

    def _observe_exchange_position_risk(self, row: dict) -> None:
        side = str(row.get("posSide") or "").lower()
        if side not in {"long", "short"}:
            return
        leverage_by_side = getattr(self, "_exchange_leverage_by_side", None)
        if not isinstance(leverage_by_side, dict):
            leverage_by_side = {"long": 0, "short": 0}
            self._exchange_leverage_by_side = leverage_by_side
        imr_by_side = getattr(self, "_exchange_position_imr_by_side", None)
        if not isinstance(imr_by_side, dict):
            imr_by_side = {"long": 0.0, "short": 0.0}
            self._exchange_position_imr_by_side = imr_by_side
        actual_leverage = int(round(safe_float(row.get("lever"))))
        leverage_by_side[side] = max(0, actual_leverage)
        imr_by_side[side] = max(0.0, safe_float(row.get("imr")))
        self._last_exchange_position_risk_ts = time.time()
        configured_leverage = int(round(safe_float(getattr(self, "_base_lev", 0))))
        if actual_leverage <= 0 or configured_leverage <= 0 or actual_leverage == configured_leverage:
            return
        now = time.time()
        if now - safe_float(getattr(self, "_last_leverage_mismatch_log_ts", 0.0)) >= 300.0:
            self.log.warning(
                f"[leverage_mismatch] {self._symbol} {side} "
                f"configured={configured_leverage} exchange={actual_leverage}"
            )
            self._last_leverage_mismatch_log_ts = now
