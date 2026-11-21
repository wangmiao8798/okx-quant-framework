"""状态持久化与盈亏记录。

从 futures_hedge.py 原样拆出，方法体逐字未改；
所有状态仍挂在 FuturesHedgeStrategy 同一实例上（多重继承共享 self）。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403


class StateMixin:
    """状态持久化与盈亏记录（10 个方法）。"""

    def _load_pnl_data(self):
        try:
            if os.path.exists(self._fee_pnl_file):
                with open(self._fee_pnl_file, "r") as f:
                    d = json.load(f)
                self._total_pnl = d.get("total_realized_pnl", 0.0)
                self._total_fees = d.get("total_fees_paid", 0.0)
                self._total_funding = d.get("total_funding_paid", 0.0)
                self._initial_equity = d.get("initial_equity", 0.0)
                self._risk_equity_baseline = d.get("risk_equity_baseline", self._initial_equity)
                try:
                    self._risk_equity_baseline_ts = safe_float(
                        d.get("risk_equity_baseline_ts", 0.0)) or os.path.getmtime(self._fee_pnl_file)
                except OSError:
                    self._risk_equity_baseline_ts = 0.0
                self._daily_pnl_date = d.get("daily_pnl_date", "")
                self._day_start_total_pnl = d.get("day_start_total_pnl", self._total_pnl)
                self._day_equity_baseline = d.get("day_equity_baseline", self._risk_equity_baseline)
                self._daily_stop_until = d.get("daily_stop_until", 0.0)
                self._daily_stop_reason = d.get("daily_stop_reason", "")
                self._daily_stop_latched = bool(d.get("daily_stop_latched", False))
                self._fill_sync_ts_ms = int(safe_float(d.get("last_fill_sync_ts_ms", 0)))
                self._recent_fill_ids = [str(x) for x in (d.get("recent_fill_ids", []) or []) if x][-200:]
                recent_side_loss_until = d.get("recent_side_loss_until", {})
                if isinstance(recent_side_loss_until, dict):
                    self._recent_side_loss_until = {
                        "long": safe_float(recent_side_loss_until.get("long", 0.0)),
                        "short": safe_float(recent_side_loss_until.get("short", 0.0)),
                    }
                position_first_seen = d.get("position_first_seen", {})
                if isinstance(position_first_seen, dict):
                    self._position_first_seen = {
                        "long": safe_float(position_first_seen.get("long", 0.0)),
                        "short": safe_float(position_first_seen.get("short", 0.0)),
                    }
                normal_close_sides = d.get("normal_close_sides", {})
                if isinstance(normal_close_sides, dict):
                    self._normal_close_sides = {
                        "long": bool(normal_close_sides.get("long", False)),
                        "short": bool(normal_close_sides.get("short", False)),
                    }
                self._tp_rehang_state = self._sanitize_tp_rehang_state(
                    d.get("tp_rehang_state")
                )
                if self._tp_rehang_state:
                    self.log.info(
                        "[TP-rehang] restored %d counter(s)"
                        % len(self._tp_rehang_state)
                    )
                self._last_pnl_save_ts_ms = self._parse_pnl_time_ms(d.get("last_update", ""))
                self.log.info(f"[PnL] 加载: realized={self._total_pnl:.2f}U fees={self._total_fees:.4f}U")
        except Exception as e:
            self.log.warning(f"[PnL] 无持久化数据, 从0开始: {e}")

    def _save_pnl_data(self):
        try:
            now = datetime.now()
            self._last_pnl_save_ts_ms = int(now.timestamp() * 1000)
            d = {
                "total_realized_pnl": round(self._total_pnl, 4),
                "total_fees_paid": round(self._total_fees, 4),
                "total_funding_paid": round(self._total_funding, 4),
                "initial_equity": round(self._initial_equity, 2),
                "risk_equity_baseline": round(self._risk_equity_baseline, 2),
                "risk_equity_baseline_ts": round(self._risk_equity_baseline_ts, 1),
                "session_start_total_pnl": round(self._session_start_total_pnl, 4),
                "daily_pnl_date": self._daily_pnl_date,
                "day_start_total_pnl": round(self._day_start_total_pnl, 4),
                "day_equity_baseline": round(self._day_equity_baseline, 2),
                "daily_stop_until": self._daily_stop_until,
                "daily_stop_reason": self._daily_stop_reason,
                "daily_stop_latched": bool(getattr(self, "_daily_stop_latched", False)),
                "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
                "last_fill_sync_ts_ms": int(self._fill_sync_ts_ms),
                "recent_fill_ids": self._recent_fill_ids[-200:],
                "recent_side_loss_until": {
                    "long": round(self._recent_side_loss_until.get("long", 0.0), 3),
                    "short": round(self._recent_side_loss_until.get("short", 0.0), 3),
                },
                "position_first_seen": {
                    "long": round(self._position_first_seen.get("long", 0.0), 3),
                    "short": round(self._position_first_seen.get("short", 0.0), 3),
                },
                "normal_close_sides": {
                    "long": bool(getattr(self, "_normal_close_sides", {}).get("long", False)),
                    "short": bool(getattr(self, "_normal_close_sides", {}).get("short", False)),
                },
                "tp_rehang_state": self._tp_rehang_state_payload(),
            }
            os.makedirs("data", exist_ok=True)
            with open(self._fee_pnl_file, "w") as f:
                json.dump(d, f, indent=2)
        except Exception as e:
            self.log.warning(f"[网格补挂] 读取现有网格失败: {e}")

    def _tp_rehang_state_payload(self) -> dict:
        """JSON-safe copy of the TP-rehang counters ({lid: n/ts/last/skip_ts})."""
        out = {}
        raw = getattr(self, "_tp_rehang_state", None) or {}
        if not isinstance(raw, dict):
            return out
        for key, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            out[str(key)] = {
                "n": int(safe_float(rec.get("n", 0))),
                "ts": round(safe_float(rec.get("ts", 0.0)), 3),
                "last": round(safe_float(rec.get("last", 0.0)), 8),
                "skip_ts": round(safe_float(rec.get("skip_ts", 0.0)), 3),
            }
        return out

    def _sanitize_tp_rehang_state(self, raw) -> dict:
        """Rebuild the counters from disk, dropping junk and expired records.

        A stale record (older than tp_rehang_state_ttl_sec) would otherwise keep
        a layer pinned at max_times forever, so it is released on load.
        """
        out = {}
        if not isinstance(raw, dict):
            return out
        ttl = max(0.0, safe_float(getattr(self, "_tp_rehang_state_ttl_sec", 0.0)))
        now = time.time()
        for key, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            ts = safe_float(rec.get("ts", 0.0))
            if ttl > 0 and ts > 0 and now - ts > ttl:
                continue
            out[str(key)] = {
                "n": max(0, int(safe_float(rec.get("n", 0)))),
                "ts": ts,
                "last": safe_float(rec.get("last", 0.0)),
                "skip_ts": safe_float(rec.get("skip_ts", 0.0)),
            }
        return out

    def _parse_pnl_time_ms(self, value) -> int:
        if value in (None, ""):
            return 0
        if isinstance(value, (int, float)):
            return int(float(value))
        try:
            return int(datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
        except Exception:
            return 0

    def _maybe_flush_pnl_heartbeat(self, force: bool = False):
        if force:
            self._save_pnl_data()
            return
        if self._pnl_heartbeat_interval <= 0:
            return
        if self._last_pnl_save_ts_ms <= 0:
            self._save_pnl_data()
            return
        now_ms = int(time.time() * 1000)
        if now_ms - self._last_pnl_save_ts_ms >= int(self._pnl_heartbeat_interval * 1000):
            self._save_pnl_data()

    def _session_realized_pnl(self) -> float:
        return self._total_pnl - self._session_start_total_pnl

    def _daily_realized_pnl(self) -> float:
        return self._total_pnl - self._day_start_total_pnl

    def _mark_side_realized_pnl(self, side: str, net_pnl: float):
        side = str(side or "").lower()
        if side not in {"long", "short"}:
            return
        if not isinstance(getattr(self, "_recent_side_loss_until", None), dict):
            self._recent_side_loss_until = {"long": 0.0, "short": 0.0}
        if net_pnl < 0:
            cooldown = max(300.0, min(900.0, abs(net_pnl) * 300.0))
            self._recent_side_loss_until[side] = max(
                self._recent_side_loss_until.get(side, 0.0),
                time.time() + cooldown,
            )

    def _clear_side_runtime_state(self, side: str, reason: str = ""):
        if side not in {"long", "short"}:
            return
        pos = self._long if side == "long" else self._short
        if pos.qty <= 0 and pos.entry <= 0 and pos.cost <= 0:
            return
        for lid in list(self._layer_tps.keys()):
            if self._tp_lid_side(lid) == side:
                self._cancel_tp_order(lid)
                self._layer_tps.pop(lid, None)
                self._layer_qty_map.pop(lid, None)
                self._layer_side_map.pop(lid, None)
        if side == "long":
            self._long = Pos("long")
            self._long_layer = 0
            self._long_add_trigger_price = 0.0
        else:
            self._short = Pos("short")
            self._short_layer = 0
            self._short_add_trigger_price = 0.0
        self._clear_segments(side)
        self.log.info(f"[持仓同步] 清理{side}侧本地状态 reason={reason or 'exchange_missing'}")

    def _calc_pnl(self, price: float) -> float:
        from strategies.hedge_calculations import calc_pnl
        return calc_pnl(
            self._long.qty, self._long.entry,
            self._short.qty, self._short.entry, price,
        )

    def _position_unrealized_pnl(self, side: str) -> float:
        from strategies.hedge_calculations import position_unrealized_pnl
        return position_unrealized_pnl(
            side, self._last_price,
            self._long.qty, self._long.entry,
            self._short.qty, self._short.entry,
            self._ct_val,
        )
