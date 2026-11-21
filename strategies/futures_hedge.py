#!/usr/bin/env python3
"""Enhanced futures hedge strategy with Opbot-style controls + Fee/PnL/Balance tracking

2026-10-18 拆分：原 6517 行巨石按领域拆为 strategies/hedge_kit/ 下 7 个 mixin，
主类仅保留生命周期入口（__init__/on_tick/reload_config）与未归类流程方法。
所有状态仍在同一实例上，行为与拆分前逐字等价。
"""
from strategies.hedge_kit.common import *  # noqa: F401,F403
from strategies.hedge_kit.tp import TpMixin
from strategies.hedge_kit.grid import GridMixin
from strategies.hedge_kit.inventory import InventoryMixin
from strategies.hedge_kit.order import OrderMixin
from strategies.hedge_kit.account import AccountMixin
from strategies.hedge_kit.risk import RiskMixin
from strategies.hedge_kit.state import StateMixin


import os as _os
import sqlite3 as _sqlite3

# ── 趋势事件记录库 ───────────────────────────────────────────────
# 用于「ADX 趋势判断命中率」线上报表的实盘触发对照：与回测理论命中率
# 并排统计 (1) MOCA 观察态判定趋势事件 (2) throttle_dominant 主导边
# 净敞口封锁事件。纯记录、绝不影响交易主流程。
_TREND_EVENTS_DB = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "data", "trend_events.db",
)


def _record_trend_event(event_type, symbol, db_path=None, **fields):
    """异常安全的趋势事件落库，绝不抛异常影响交易主流程。

    event_type:
      "moca"     -> moca_events(symbol, ts, state, confidence, apply_live)
      "throttle" -> throttle_events(symbol, ts, side, action, net, soft)
    db_path 仅测试时覆盖；生产使用模块级 _TREND_EVENTS_DB。
    """
    try:
        import time as _time
        path = db_path or _TREND_EVENTS_DB
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        conn = _sqlite3.connect(path, timeout=5)
        try:
            if event_type == "moca":
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS moca_events("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, "
                    "state TEXT, confidence REAL, apply_live INTEGER)"
                )
                conn.execute(
                    "INSERT INTO moca_events(ts, symbol, state, confidence, apply_live) "
                    "VALUES(?,?,?,?,?)",
                    (_time.time(), str(symbol), str(fields.get("state", "")),
                     float(fields.get("confidence", 0.0) or 0.0),
                     int(fields.get("apply_live", 0))),
                )
            elif event_type == "throttle":
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS throttle_events("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, symbol TEXT, "
                    "side TEXT, action TEXT, net REAL, soft REAL)"
                )
                conn.execute(
                    "INSERT INTO throttle_events(ts, symbol, side, action, net, soft) "
                    "VALUES(?,?,?,?,?,?)",
                    (_time.time(), str(symbol), str(fields.get("side", "")),
                     str(fields.get("action", "")),
                     float(fields.get("net", 0.0) or 0.0),
                     float(fields.get("soft", 0.0) or 0.0)),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


class FuturesHedgeStrategy(TpMixin, GridMixin, InventoryMixin, OrderMixin, AccountMixin, RiskMixin, StateMixin, BaseStrategy):
    TAKER_FEE = 0.0005  # 0.05%
    MAKER_FEE = 0.0002  # 0.02%

    def __init__(self, config: dict, okx_client=None):
        super().__init__(config, "futures_hedge")
        fc = config.get("futures", {})
        self._symbol = fc.get("symbol", "BTC-USDT-SWAP")
        profile = config.get("profile", {}) if isinstance(config.get("profile"), dict) else {}
        raw_slug = str(profile.get("slug") or self._symbol.split("-")[0] or "strategy").lower()
        safe_slug = "".join(ch if ch.isalnum() else "_" for ch in raw_slug)
        while "__" in safe_slug:
            safe_slug = safe_slug.replace("__", "_")
        self._profile_slug = safe_slug.strip("_") or "strategy"
        self._profile_name = str(profile.get("name") or self._symbol or self._profile_slug)
        self._is_primary_profile = bool(profile.get("is_primary", False) or self._profile_slug == "base")
        state_suffix = "" if self._is_primary_profile else f"_{self._profile_slug}"
        self._base_lev = int(safe_float(fc.get("leverage", 3)))
        self._margin_mode = fc.get("margin_mode", "cross")
        # Side-specific parameters are optional and fall back to the legacy
        # shared values.  Keeping the fallback preserves existing profiles
        # while allowing long/short risk budgets to diverge deliberately.
        self._side_config = {
            side: (fc.get(side, {}) if isinstance(fc.get(side, {}), dict) else {})
            for side in ("long", "short")
        }
        self._base_pos_usdt = safe_float(fc.get("base_position_usdt", 50))
        self._max_adds = int(safe_float(fc.get("max_add_layers", 5)))
        # Hard ceiling on grid layers per side. Historically hardcoded to 3 in
        # _target_grid_layers(); exposed so a profile can widen its ladder.
        self._grid_max_layers = max(1, int(safe_float(fc.get("grid_max_layers", 3))))
        # Wick filter blocks counter-trend adds inside impulsive candles, which is
        # the opposite of what a mean-reversion grid needs. Opt-out per profile.
        self._wick_filter_enabled = bool(fc.get("wick_filter_enabled", True))
        # When both hedge sides are within this relative gap of each other there is
        # no meaningful dominant side, so THROTTLED must not veto either side.
        self._throttle_dominant_gap_ratio = max(0.0, safe_float(fc.get("throttle_dominant_gap_ratio", 0.0)))
        # THROTTLED account mode should only veto the dominant side once this
        # symbol has spent its own net-exposure budget. Judging purely by
        # intra-symbol inventory freezes any profile that is one-sided inside
        # its own caps (RAM: net -238 against a 300 soft cap), which starves
        # the exact leg that would rebuild the hedge. Opt-in per profile.
        self._throttle_dominant_net_budget_gate_enabled = bool(
            fc.get("throttle_dominant_net_budget_gate_enabled", False)
        )
        self._add_step = safe_float(fc.get("add_step", 0.02))
        self._add_mult = safe_float(fc.get("add_multiplier", 1.3))
        self._first_tp = safe_float(fc.get("first_tp_ratio", 0.02))
        self._final_tp = safe_float(fc.get("final_tp_ratio", 0.05))
        self._trail = safe_float(fc.get("trailing_tp_retreat", 0.015))
        if self._trail >= 1.0:
            self._trail = max(0.001, self._trail - 1.0)
        self._min_edge_ratio = safe_float(fc.get("min_edge_ratio", 0.0035))
        self._min_expected_net_usdt = safe_float(fc.get("min_expected_net_usdt", 0.05))
        self._trend_bias_strength = safe_float(fc.get("trend_bias_strength", 1.15))
        self._skew_boost_strength = safe_float(fc.get("skew_boost_strength", 0.55))
        self._skew_reduce_strength = safe_float(fc.get("skew_reduce_strength", 0.65))
        self._inventory_age_hours = safe_float(fc.get("inventory_age_hours", 3.0))
        self._inventory_loss_usdt = safe_float(fc.get("inventory_loss_usdt", 3.0))
        self._inventory_age_reduce_strength = safe_float(fc.get("inventory_age_reduce_strength", 0.45))
        self._inventory_age_sizing_enabled = bool(fc.get("inventory_age_sizing_enabled", True))
        self._recent_side_loss_guard_enabled = bool(fc.get("recent_side_loss_guard_enabled", True))
        self._first_tp = max(self._first_tp, self._min_edge_ratio)
        self._final_tp = max(self._final_tp, self._first_tp + 0.002)
        self._compound = safe_float(fc.get("auto_compound_ratio", 0.5))
        self._cooldown = safe_float(fc.get("add_cooldown", 120))
        self._add_rate_limit_window_sec = max(30.0, safe_float(fc.get("add_rate_limit_window_sec", 300)))
        self._regular_add_rate_limit_margin_usdt = max(
            0.0, safe_float(fc.get("regular_add_rate_limit_margin_usdt", 15))
        )
        self._inventory_repair_rate_limit_margin_usdt = max(
            0.0, safe_float(fc.get("inventory_repair_rate_limit_margin_usdt", 45))
        )
        self._paired_add_rate_limit_margin_usdt = max(
            0.0, safe_float(fc.get("paired_add_rate_limit_margin_usdt", 45))
        )
        self._client = okx_client
        self._account_state = AccountRiskState(self._client, self.log)
        self._account_budget_allocator = None
        self._execution_gateway = ExecutionGateway(okx_client) if okx_client else None
        if self._execution_gateway:
            self._execution_gateway.set_risk_context_provider(self._risk_context_snapshot)
        self._inventory = HedgeInventory()
        self._total_pnl = 0.0; self._peak_pnl = 0.0
        self._first_tp_done = False; self._init_done = False
        self._last_add = 0; self._cur_lev = self._base_lev
        self._last_price = 0.0; self._min_qty = 1; self._qty_step = 1; self._tick_size = 0.0
        self._ct_val = 0.0
        self._instrument_info_ready = okx_client is None
        self._base_first_tp = self._first_tp
        self._base_final_tp = self._final_tp
        self._base_max_adds = self._max_adds
        self._max_long_pos = int(safe_float(fc.get("max_long_pos", 5)))
        self._max_short_pos = int(safe_float(fc.get("max_short_pos", 5)))
        self._max_side_notional_usdt = max(0.0, safe_float(fc.get("max_side_notional_usdt", 0)))
        # A legacy shared cap remains the fallback. Explicit per-side caps let
        # independent long/short grids carry different bounded inventories.
        self._max_long_side_notional_usdt = max(0.0, safe_float(fc.get("max_long_side_notional_usdt", 0)))
        self._max_short_side_notional_usdt = max(0.0, safe_float(fc.get("max_short_side_notional_usdt", 0)))
        self._max_total_notional_usdt = max(0.0, safe_float(fc.get("max_total_notional_usdt", 0)))
        self._max_net_notional_usdt = max(0.0, safe_float(fc.get("max_net_notional_usdt", 0)))
        self._max_net_notional_soft_usdt = max(
            0.0,
            safe_float(
                fc.get(
                    "max_net_notional_soft_usdt",
                    self._max_net_notional_usdt * 0.5,
                )
            ),
        )
        # TP rehang: pull stranded layer TPs back toward cost instead of
        # leaving them pinned at historical layer entries. Dry-run first.
        self._tp_rehang_enabled = bool(fc.get("tp_rehang_enabled", False))
        self._tp_rehang_dry_run = bool(fc.get("tp_rehang_dry_run", True))
        self._tp_rehang_min_gap_ratio = max(
            0.002, safe_float(fc.get("tp_rehang_min_gap_ratio", 0.02))
        )
        self._tp_rehang_step_ratio = min(
            1.0, max(0.05, safe_float(fc.get("tp_rehang_step_ratio", 0.5)))
        )
        self._tp_rehang_cooldown_sec = max(
            60.0, safe_float(fc.get("tp_rehang_cooldown_sec", 900))
        )
        self._tp_rehang_max_times = max(
            1, int(safe_float(fc.get("tp_rehang_max_times", 4)))
        )
        # Price-band clamp for take-profit orders (OKX 51006).
        self._tp_price_band_enabled = bool(fc.get("tp_price_band_enabled", True))
        self._tp_price_band_gap_ratio = max(
            0.0, safe_float(fc.get("tp_price_band_gap_ratio", 0.003))
        )
        self._tp_rehang_net_headroom = min(
            1.0, max(0.1, safe_float(fc.get("tp_rehang_net_headroom", 0.8)))
        )
        self._tp_rehang_state_ttl_sec = max(
            0.0, safe_float(fc.get("tp_rehang_state_ttl_hours", 24)) * 3600.0
        )
        self._tp_rehang_state = {}

        self._inventory_hard_hedge_enabled = bool(
            fc.get("inventory_hard_hedge_enabled", False)
        )
        self._inventory_hard_hedge_confirmations = max(
            1, int(safe_float(fc.get("inventory_hard_hedge_confirmations", 2)))
        )
        self._inventory_hard_hedge_cooldown_sec = max(
            3.0, safe_float(fc.get("inventory_hard_hedge_cooldown_sec", 60))
        )
        self._inventory_hard_hedge_max_notional_usdt = max(
            0.0, safe_float(fc.get("inventory_hard_hedge_max_notional_usdt", 150))
        )
        self._inventory_hard_breach_count = 0
        self._last_inventory_hard_hedge_ts = 0.0
        self._dynamic_side_notional_enabled = bool(fc.get("dynamic_side_notional_enabled", True))
        self._dynamic_side_notional_equity_ratio = max(
            0.0, safe_float(fc.get("dynamic_side_notional_equity_ratio", 0.0))
        )
        self._dynamic_side_notional_max_multiplier = max(
            1.0, safe_float(fc.get("dynamic_side_notional_max_multiplier", 1.4))
        )
        self._dynamic_side_notional_margin_budget_ratio = min(
            1.0, max(0.0, safe_float(fc.get("dynamic_side_notional_margin_budget_ratio", 0.80)))
        )
        self._dynamic_side_notional_max_margin_ratio = min(
            0.8, max(0.0, safe_float(fc.get("dynamic_side_notional_max_margin_ratio", 0.35)))
        )
        self._inventory_repair_side_cap_multiplier = max(
            1.0, safe_float(fc.get("inventory_repair_side_cap_multiplier", 1.0))
        )
        self._inventory_repair_max_margin_ratio = min(
            0.8, max(0.0, safe_float(fc.get("inventory_repair_max_margin_ratio", 0.35)))
        )
        self._active_inventory_repair_enabled = bool(fc.get("active_inventory_repair_enabled", False))
        # A narrowly scoped CRV repair lane may cross the shared account hard
        # line by one small step. The final decision remains in RiskPolicy;
        # these values only make the strategy and gateway use the same input.
        self._inventory_repair_hard_override_enabled = bool(
            fc.get("inventory_repair_hard_override_enabled", False)
        )
        self._inventory_repair_hard_override_symbol = str(
            fc.get("inventory_repair_hard_override_symbol") or ""
        ).strip().upper()
        self._inventory_repair_hard_override_margin_ratio = min(
            0.75,
            max(0.0, safe_float(fc.get("inventory_repair_hard_override_margin_ratio", 0.0))),
        )
        self._inventory_repair_hard_override_mgn_ratio_min = max(
            0.0,
            safe_float(fc.get("inventory_repair_hard_override_mgn_ratio_min", 0.0)),
        )
        self._inventory_repair_hard_override_max_step_notional_usdt = max(
            0.0,
            safe_float(
                fc.get("inventory_repair_hard_override_max_step_notional_usdt", 0.0)
            ),
        )
        self._inventory_repair_hard_override_last = {}
        self._inventory_repair_ignore_layer_cap = bool(fc.get("inventory_repair_ignore_layer_cap", False))
        self._inventory_repair_relax_edge_enabled = bool(fc.get("inventory_repair_relax_edge_enabled", False))
        self._inventory_repair_imbalance_gate = min(
            1.0, max(0.0, safe_float(fc.get("inventory_repair_imbalance_gate", 0.70)))
        )
        self._inventory_repair_stage_ratio = min(
            0.5, max(0.05, safe_float(fc.get("inventory_repair_stage_ratio", 0.30)))
        )
        self._inventory_repair_exit_net_notional_usdt = max(
            0.0,
            safe_float(fc.get("inventory_repair_exit_net_notional_usdt", self._max_net_notional_usdt * 0.8)),
        )
        self._inventory_repair_exit_imbalance_ratio = min(
            1.0, max(0.0, safe_float(fc.get("inventory_repair_exit_imbalance_ratio", 0.45)))
        )
        self._inventory_repair_cooldown_sec = max(
            30.0, safe_float(fc.get("inventory_repair_cooldown_sec", 120))
        )
        self._inventory_repair_fast_cooldown_sec = max(
            15.0, safe_float(fc.get("inventory_repair_fast_cooldown_sec", 30))
        )
        self._inventory_repair_medium_cooldown_sec = max(
            self._inventory_repair_fast_cooldown_sec,
            safe_float(fc.get("inventory_repair_medium_cooldown_sec", 60)),
        )
        # CRV-only inventory cleanup.  This is deliberately opt-in because
        # ordinary grid profiles must never gain an implicit market-unwind path.
        self._over_cap_unwind_enabled = bool(fc.get("over_cap_unwind_enabled", False))
        self._over_cap_unwind_target_ratio = min(
            0.98, max(0.70, safe_float(fc.get("over_cap_unwind_target_ratio", 0.90)))
        )
        self._over_cap_unwind_stage_ratio = min(
            0.50, max(0.05, safe_float(fc.get("over_cap_unwind_stage_ratio", 0.15)))
        )
        self._over_cap_unwind_max_notional_usdt = max(
            0.0, safe_float(fc.get("over_cap_unwind_max_notional_usdt", 36.0))
        )
        self._over_cap_unwind_min_notional_usdt = max(
            0.0, safe_float(fc.get("over_cap_unwind_min_notional_usdt", 6.0))
        )
        self._over_cap_unwind_cooldown_sec = max(
            30.0, safe_float(fc.get("over_cap_unwind_cooldown_sec", 60.0))
        )
        self._over_cap_unwind_mode = False
        self._last_over_cap_unwind_ts = 0.0
        self._over_cap_unwind_last_reason = ""
        self._over_cap_unwind_last_result = ""
        # FART may use account-margin-first capacity.  This softens only the
        # per-symbol gross caps; account budget, margin health, net exposure,
        # TP coverage, edge and hard-stop checks remain authoritative.
        self._margin_first_capacity_enabled = bool(fc.get("margin_first_capacity_enabled", False))
        self._margin_first_max_margin_ratio = min(
            0.70, max(0.05, safe_float(fc.get("margin_first_max_margin_ratio", 0.50)))
        )
        self._margin_first_normal_margin_ratio = min(
            self._margin_first_max_margin_ratio,
            max(0.05, safe_float(fc.get("margin_first_normal_margin_ratio", self._margin_first_max_margin_ratio))),
        )
        self._margin_first_reduced_size_ratio = min(
            1.0, max(0.10, safe_float(fc.get("margin_first_reduced_size_ratio", 0.50)))
        )
        self._margin_first_ignore_floating_loss = bool(
            fc.get("margin_first_ignore_floating_loss", False)
        )
        self._margin_first_relax_edge_enabled = bool(
            fc.get("margin_first_relax_edge_enabled", False)
        )
        # A paired add is supplemental for opted-in hedge profiles. It must not
        # replace an otherwise fundable maker grid on the dominant side.
        self._hedged_grid_allow_dominant_refill = bool(
            fc.get("hedged_grid_allow_dominant_refill", False)
        )
        self._paired_add_enabled = bool(fc.get("paired_add_enabled", False))
        self._paired_add_use_hard_total_cap = bool(fc.get("paired_add_use_hard_total_cap", False))
        self._paired_average_down_enabled = bool(
            fc.get("paired_average_down_enabled", False)
        )
        self._paired_add_ignore_layer_cap = bool(
            fc.get("paired_add_ignore_layer_cap", False)
        )
        self._paired_add_cooldown_sec = max(30.0, safe_float(fc.get("paired_add_cooldown_sec", 120)))
        self._paired_add_trigger_ratio = max(0.001, safe_float(fc.get("paired_add_trigger_ratio", 0.0075)))
        self._paired_add_ignore_wick = bool(fc.get("paired_add_ignore_wick", False))
        self._paired_add_max_margin_ratio = min(
            0.50, max(0.0, safe_float(fc.get("paired_add_max_margin_ratio", 0.0)))
        )
        self._paired_add_allow_inventory_pause = bool(fc.get("paired_add_allow_inventory_pause", False))
        self._conditional_missing_side_repair_enabled = bool(
            fc.get("conditional_missing_side_repair_enabled", False)
        )
        self._missing_side_repair_allow_legacy_state = bool(
            fc.get("missing_side_repair_allow_legacy_state", False)
        )
        self._missing_side_repair_imbalance_threshold = min(
            1.0, max(0.05, safe_float(fc.get("missing_side_repair_imbalance_threshold", 0.70)))
        )
        self._missing_side_repair_cooldown_sec = max(
            30.0, safe_float(fc.get("missing_side_repair_cooldown_sec", 180))
        )
        self._missing_side_repair_max_margin_ratio = min(
            0.8, max(0.05, safe_float(fc.get("missing_side_repair_max_margin_ratio", 0.30)))
        )
        self._working_total_notional_ratio = min(
            1.0, max(0.5, safe_float(fc.get("working_total_notional_ratio", 1.0)))
        )
        self._inventory_margin_stop_ratio = min(
            0.8, max(0.0, safe_float(fc.get("inventory_margin_stop_ratio", 0.35)))
        )
        self._inventory_margin_resume_ratio = min(
            self._inventory_margin_stop_ratio,
            max(0.0, safe_float(fc.get("inventory_margin_resume_ratio", 0.30))),
        )
        # A one-sided DRAM inventory needs a bounded hedge-repair allowance;
        # otherwise the normal gross-margin pause can prevent reopening the
        # missing side even while account hard-margin and mgn-ratio checks are
        # still healthy.
        self._inventory_repair_missing_side_margin_ratio = min(
            0.70,
            max(0.0, safe_float(fc.get("inventory_repair_missing_side_margin_ratio", 0.50))),
        )
        self._inventory_repair_missing_side_resume_ratio = min(
            self._inventory_repair_missing_side_margin_ratio,
            max(0.0, safe_float(fc.get("inventory_repair_missing_side_resume_ratio", 0.45))),
        )
        self._inventory_margin_hedge_credit_ratio = min(
            0.25,
            max(0.0, safe_float(fc.get("inventory_margin_hedge_credit_ratio", 0.20))),
        )
        self._margin_buffer_ratio = min(
            0.50, max(0.0, safe_float(fc.get("account_margin_buffer_ratio", 0.10)))
        )
        self._mgn_ratio_open_limit = max(
            0.0, safe_float(fc.get("mgn_ratio_open_limit", 5.0))
        )
        self._mgn_ratio_pause_limit = max(
            self._mgn_ratio_open_limit,
            safe_float(fc.get("mgn_ratio_pause_limit", 3.0)),
        )
        self._mgn_ratio_emergency_limit = max(
            0.0, min(self._mgn_ratio_pause_limit, safe_float(fc.get("mgn_ratio_emergency_limit", 2.0)))
        )
        # In hedge mode OKX may omit per-side liqPx. Account-level margin data
        # is the authoritative opening guard, and must be fresh before adding
        # to an existing position.
        self._account_risk_max_age_sec = max(
            5.0, safe_float(fc.get("account_risk_max_age_sec", 15.0))
        )
        self._last_inventory_repair_ts = 0.0
        self._last_paired_add_ts = 0.0
        self._last_missing_side_repair_ts = 0.0
        self._missing_side_repair_last_reason = ""
        self._inventory_repair_mode = False
        self._inventory_margin_paused = False
        self._paired_add_anchor_price = 0.0
        self._paired_add_anchor_side = ""
        self._max_margin_ratio = safe_float(fc.get("max_margin_ratio", 0.55))
        self._account_margin_soft_limit = min(0.95, max(0.0, safe_float(fc.get("account_margin_soft_limit", self._max_margin_ratio))))
        self._account_margin_hard_limit = min(0.99, max(self._account_margin_soft_limit, safe_float(fc.get("account_margin_hard_limit", self._max_margin_ratio))))
        self._account_margin_resume_limit = min(self._account_margin_hard_limit, max(0.0, safe_float(fc.get("account_margin_resume_limit", self._account_margin_soft_limit))))
        self._account_margin_scope = str(fc.get("account_margin_scope") or "shared_okx_account")
        self._net_reducing_hard_margin_exempt_enabled = bool(
            fc.get("net_reducing_hard_margin_exempt_enabled", False)
        )
        self._min_open_equity_usdt = max(0.0, safe_float(fc.get("min_open_equity_usdt", 0.0)))
        self._weak_side_margin_ratio = safe_float(fc.get("weak_side_margin_ratio", self._max_margin_ratio))
        self._weak_side_margin_imbalance_threshold = safe_float(fc.get("weak_side_margin_imbalance_threshold", 0.25))
        self._reserved_usdt = max(0.0, safe_float(fc.get("reserved_usdt", 20)))
        # 2026-10-07 风控放宽：多空不对称属正常波动，浮亏只作弱信号。
        # 冻结阈值 0.50 -> 1.50（净浮亏达多空总成本 150% 才停主导侧扩张）；
        # 缩量阈值 0.30 -> 0.60。净敞口硬上限(max_net_notional_usdt)保留为最终防线。
        self._floating_loss_shrink = safe_float(fc.get("floating_loss_shrink", 0.60))
        self._floating_loss_freeze = safe_float(fc.get("floating_loss_freeze", 1.50))
        self._floating_loss_ratio = 1.0
        self._floating_loss_freeze_active = False
        self._candle_anti_dup = fc.get("candle_anti_duplicate", True)
        self._last_add_candle = ""; self._current_candle_id = ""
        self._price_dedup = fc.get("price_deduplication", True)
        self._breaker_until = 0.0
        self._breaker_cooldown = safe_float(fc.get("breaker_cooldown", 10))
        self._layer_tps = {}
        self._layer_qty_map = {}  # lid -> exact qty for layered TP
        self._layer_side_map = {}  # lid -> side for layered TP; do not infer from lid parity
        self._layer_tp_orders = {}  # lid -> order_id (挂限价止盈单)
        # A reduce-only rejection with code 51169 means the exchange has no
        # position for that side. Suppress immediate TP recreation until the
        # next position reconciliation has a chance to become authoritative.
        self._tp_no_position_until = {"long": 0.0, "short": 0.0}
        self._next_layer_id = 0
        self._base_tp_ratio = self._first_tp
        # === 下单冷却: 同方向3秒冷却 ===
        self._last_add_long = 0
        self._last_add_short = 0
        self._add_cooldown_sec = int(safe_float(fc.get("add_cooldown_sec", 3)))
        self._refresh_tp_on_startup = bool(fc.get("refresh_tp_on_startup", False))
        self._last_startup_recovery_ts = 0.0
        # Coverage repair is a bounded invariant repair, not a profit-taking path.
        self._coverage_safety_tp_enabled = bool(fc.get("coverage_safety_tp_enabled", False))
        self._coverage_safety_tp_max_gap_ratio = min(0.25, max(0.0, safe_float(fc.get("coverage_safety_tp_max_gap_ratio", 0.10))))
        self._coverage_safety_tp_max_loss_usdt = max(0.0, safe_float(fc.get("coverage_safety_tp_max_loss_usdt", 0.05)))
        self._coverage_safety_tp_repair_limit = max(0, int(safe_float(fc.get("coverage_safety_tp_repair_limit", 1))))
        self._coverage_safety_tp_repair_incident = {"long": False, "short": False}
        self._coverage_safety_tp_repair_remaining = {"long": 0, "short": 0}
        self._coverage_safety_tp_repair_position_qty = {"long": 0.0, "short": 0.0}
        self._coverage_safety_tp_state_file = state_storage_path(config, f"coverage_safety_tp{state_suffix}.json")
        self._load_coverage_safety_tp_state()
        # === 净敞口自动纠偏 ===
        # Deprecated unit-based net exposure gate. Runtime opening decisions
        # use max_net_notional_usdt; keep an explicit opt-in only for legacy
        # callers and tests, never as a default production limit.
        self._max_net_exposure = max(0.0, safe_float(fc.get("max_net_exposure", 0.0)))
        self._grid_bias_ratio = safe_float(fc.get("grid_bias_ratio", 0.22))
        self._grid_atr_enabled = bool(fc.get("grid_atr_enabled", False))
        self._grid_atr_multiplier = max(0.0, safe_float(fc.get("grid_atr_multiplier", 0.0)))
        self._grid_atr_min_step = max(0.0001, safe_float(fc.get("grid_atr_min_step", 0.0008)))
        self._grid_atr_max_step = max(
            self._grid_atr_min_step,
            safe_float(fc.get("grid_atr_max_step", 0.02)),
        )
        self._grid_range_enabled = bool(fc.get("grid_range_enabled", False))
        self._grid_lower_price = max(0.0, safe_float(fc.get("grid_lower_price", 0.0)))
        self._grid_upper_price = max(0.0, safe_float(fc.get("grid_upper_price", 0.0)))
        self._grid_boundary_buffer_ratio = min(
            0.05,
            max(0.0, safe_float(fc.get("grid_boundary_buffer_ratio", 0.002))),
        )
        self._grid_boundary_action = str(
            fc.get("grid_boundary_action", "pause") or "pause"
        ).strip().lower()
        self._post_recovery_refill_delay = 0.0
        self._post_recovery_grid_layers = int(safe_float(fc.get("post_recovery_grid_layers", 1)))
        self._grid_expand_margin_ratio = safe_float(fc.get("grid_expand_margin_ratio", 0.22))
        self._reduce_only_mode = bool(fc.get("reduce_only_mode", False))
        self._last_reduce_only_cancel = 0.0
        self._hard_stop_enabled = fc.get("hard_stop_enabled", False)
        self._max_loss_usdt = int(safe_float(fc.get("max_loss_usdt", 20)))
        self._max_drawdown = safe_float(fc.get("max_drawdown", 0.15))
        # ====== 权益基线健全性守护（防陈旧基线导致熔断失效/假熔断）======
        self._baseline_max_age_days = safe_float(fc.get("baseline_max_age_days", 7))
        self._baseline_reanchor_drift = safe_float(fc.get("baseline_reanchor_drift", 0.30))
        self._risk_equity_baseline_ts = 0.0
        self._baseline_stale_log_ts = 0.0
        self._recovery_minutes = 0.0
        self._recovery_pos_multiplier = 1.0
        self._startup_recovery_seconds = 0.0
        self._startup_auto_hedge_missing_side = fc.get("startup_auto_hedge_missing_side", False)
        self._startup_rebuild_grids = fc.get("startup_rebuild_grids", False)
        self._daily_loss_limit_usdt = safe_float(fc.get("daily_loss_limit_usdt", max(8, self._max_loss_usdt * 0.8)))
        self._daily_drawdown = safe_float(fc.get("daily_drawdown", min(self._max_drawdown, 0.06)))
        self._session_started_at = time.time()
        self._funding_adjusted = False
        self._funding_adjust_qty = 0
        self._funding_adjust_side = ""
        self._funding_adjust_ts = 0  # timestamp of last reduce
        self._max_tp_orders = int(safe_float(fc.get("max_tp_orders", 10)))
        # Soft TP cap may be exceeded only by a small, explicit safety allowance
        # when live position growth would otherwise remain uncovered.
        self._max_tp_orders_hard = max(
            self._max_tp_orders,
            int(safe_float(fc.get("max_tp_orders_hard", self._max_tp_orders + 2))),
        )
        self._tp_compact_target_orders = max(
            1, min(5, int(safe_float(fc.get("tp_compact_target_orders", 5))))
        )
        self._tp_funding_buffer_ratio = max(
            0.0, safe_float(fc.get("tp_funding_buffer_ratio", 0.001))
        )
        self._tp_init_done = False
        self._active_tp_orders = 0
        self._exchange_tp_coverage_qty = {"long": 0.0, "short": 0.0}
        self._exchange_tp_coverage_ready = False
        self._exchange_tp_coverage_at = 0.0
        self._last_tp_compact_ts = 0.0
        self._tp_compact_candidate_signature = None
        self._tp_compact_candidate_since = 0.0
        self._tp_compaction_in_progress = False
        self._tp_compaction_pending = None
        self._tp_compaction_retry_after = 0.0
        self._tp_compaction_failures = 0
        self._last_position_change_ts = 0.0
        self._last_restored_tp_signature = None
        self._consecutive_errors = 0
        self._order_records = []
        self._grid_center_price = 0.0
        self._drift_threshold = 0.015
        self._last_rebuild_time = 0.0
        self._rebuild_cooldown = 30.0
        self._sync_rebuild_cooldown = 8.0
        self._order_refresh_tolerance_pct = safe_float(fc.get("order_refresh_tolerance_pct", 0.0025))
        self._order_cancel_cooldown_sec = safe_float(fc.get("order_cancel_cooldown_sec", 45))
        self._last_grid_cancel_ts = {}
        self._last_sync_rebuild_time = 0.0
        self._last_short_grid_px = 0.0
        self._last_short_grid_ts = 0.0
        self._long_grid_pause_until = 0.0
        self._short_grid_pause_until = 0.0
        self._grid_pause_reasons = {"long": "", "short": ""}
        self._risk_opening_pause_until = 0.0
        self._risk_opening_pause_reason = ""
        self._last_short_grid_fail_px = 0.0
        self._last_short_grid_fail_ts = 0.0
        self._last_short_grid_fail_code = ""
        self._short_grid_fail_cooldown = 45.0
        self._grid_refill_block_log_state = {}
        self._grid_refill_block_log_interval = 300.0
        self._opening_guard_log_state = {}
        self._opening_guard_events = []
        self._opening_guard_observe_window_sec = 900.0
        self._startup_sync_ts = 0.0
        self._startup_recovery_active = False
        self._startup_recovery_ended_at = 0.0
        self._long_layer = 0
        self._short_layer = 0
        self._last_calibrate = 0.0
        self._calibrate_interval = 30
        self._last_calibrate_tick = 0
        self._use_opbot_grid = True
        # 熔断风控
        self._price_history = []
        self._circuit_breaker_cd = int(safe_float(fc.get("circuit_breaker_cd", 1800)))
        self._cb_pause_until = 0.0
        self._cb_close_until = 0.0
        # 插针过滤
        self._wick_px = 0.0
        self._wick_ts = 0.0
        self._wick_side = ""
        moca_cfg = config.get("moca", {}) if isinstance(config.get("moca"), dict) else {}
        moca_thresholds = moca_cfg.get("thresholds", {}) if isinstance(moca_cfg.get("thresholds"), dict) else {}
        self._moca_wick_ratio = max(1.2, safe_float(moca_thresholds.get("wick_ratio", 2.5)))
        self._moca_apply_live = bool(moca_cfg.get("apply_live", False))
        self._moca_state = ""
        self._moca_observed_state = ""
        self._moca_observed_confidence = 0.0
        self._moca_atr_ratio = 0.0
        self._moca_grid_step_multiplier = 1.0
        self._long_grid_levels = []
        self._short_grid_levels = []
        self._recent_side_loss_until = {"long": 0.0, "short": 0.0}
        # A side reduced by a normal TP must not be recreated as a "missing" hedge.
        self._normal_close_sides = {"long": False, "short": False}
        # ====== 新增: 手续费 + PnL持久化 + 余额实盘监控 ======
        self._total_fees = 0.0
        self._total_funding = 0.0
        self._initial_equity = 0.0
        self._risk_equity_baseline = 0.0
        self._session_start_total_pnl = 0.0
        self._daily_pnl_date = ""
        self._day_start_total_pnl = 0.0
        self._day_equity_baseline = 0.0
        self._daily_stop_until = 0.0
        self._daily_stop_reason = ""
        self._daily_stop_latched = False
        # The child process owns accounting files under its tenant directory;
        # main profiles keep their established data/ paths.
        self._fee_pnl_file = state_storage_path(config, f"fee_pnl{state_suffix}.json")
        self._last_pnl_save_ts_ms = 0
        self._fill_sync_ts_ms = 0
        self._recent_fill_ids = []
        self._last_fill_sync = 0.0
        self._position_first_seen = {"long": 0.0, "short": 0.0}
        self._fill_sync_interval = safe_float(fc.get("fill_sync_interval", 15))
        self._pnl_heartbeat_interval = safe_float(fc.get("pnl_heartbeat_interval", 60))
        self._halt_state_file = state_storage_path(config, f"halt_state{state_suffix}.json")
        self._pnl_csv_path = state_storage_path(config, f"pnl_records{state_suffix}.csv")
        self._load_pnl_data()
        self._session_start_total_pnl = self._total_pnl
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl_date = today
            self._day_start_total_pnl = self._total_pnl
            self._day_equity_baseline = self._risk_equity_baseline
            if not bool(getattr(self, "_daily_stop_latched", False)):
                self._daily_stop_until = 0.0
                self._daily_stop_reason = ""
            self._save_pnl_data()
        # ====== 分段大额预埋平仓单 ======
        sc = config.get("segment_close", {})
        self._seg_enable = sc.get("enable", True)
        self._seg_layer_limit = int(sc.get("segment_layer_limit", 3))
        self._seg_close_offset = safe_float(sc.get("close_offset", 0.01))
        self._seg_price_buffer = safe_float(sc.get("price_buffer", 0.003))
        self._seg_refresh_ms = sc.get("order_refresh_ms", 2000)
        self._segments = {"long": [], "short": []}
        self._last_seg_refresh = 0.0
        self._layer_history = {"long": [], "short": []}
        self._base_add_step = self._add_step
        self._base_add_mult = self._add_mult
        self._trading_halted = False
        self._halt_reason = ""
        self._resume_required = False
        self._halt_operator = ""
        if bool(getattr(self, "_daily_stop_latched", False)):
            self._trading_halted = True
            self._halt_reason = self._daily_stop_reason or "daily_stop_persisted"
            self._resume_required = True
        self._last_flatten_attempt = 0.0
        self._flatten_cooldown = 15.0
        self._halt_updated_at = ""
        self._last_event_type = ""
        self._last_event_side = ""
        self._last_event_reason = ""
        self._last_event_detail = ""
        self._last_event_price = 0.0
        self._last_event_qty = 0.0
        self._last_event_ts = 0.0
        self._last_event_fp = ()
        self._long_add_trigger_price = 0.0
        self._short_add_trigger_price = 0.0
        self._long_add_block_reason = ""
        self._short_add_block_reason = ""
        self._long_add_cooldown_left = 0.0
        self._short_add_cooldown_left = 0.0
        self._global_add_block_reason = ""
        self._global_add_cooldown_left = 0.0
        self._liquidation_status = {
            "available": False,
            "positions_present": False,
            "zone": "unavailable",
            "liq_px_unavailable": False,
            "liquidation_distance_safe": False,
            "liquidation_warning": False,
            "liquidation_emergency": False,
            "min_distance_rate": None,
            "reason": "not_checked",
            "opening_pause_active": False,
            "opening_pause_reason": "",
            "opening_pause_remaining_sec": 0.0,
        }
        self._position_missing_counts = {"long": 0, "short": 0}
        self._exchange_leverage_by_side = {"long": 0, "short": 0}
        self._exchange_position_imr_by_side = {"long": 0.0, "short": 0.0}
        self._last_exchange_position_risk_ts = 0.0
        self._last_position_query_ok = True
        self._last_leverage_mismatch_log_ts = 0.0
        if not isinstance(getattr(self, "_position_first_seen", None), dict):
            self._position_first_seen = {"long": 0.0, "short": 0.0}
        else:
            self._position_first_seen = {
                "long": safe_float(self._position_first_seen.get("long", 0.0)),
                "short": safe_float(self._position_first_seen.get("short", 0.0)),
            }
        self._order_tracker = OrderTracker(self._symbol)
        self._execution_guard = ExecutionGuard(self._client) if self._client else None
        self._last_order_tracker_log = 0.0
        opening_order_id = ""
        if self._client:
            try:
                self.update_info(self._client.get_futures_info(self._symbol))
            except Exception as e:
                self._instrument_info_ready = False
                self.log.error(f"[instrument_guard] {self._symbol} unavailable; opening disabled: {e}")
        self._load_halt_state()

    def update_info(self, info: dict):
        min_qty = safe_float(info.get("minSz", 0))
        qty_step = safe_float(info.get("lotSz", 0))
        tick_size = safe_float(info.get("tickSz", 0))
        ct_val = safe_float(info.get("ctVal", 0))
        if not info.get("ok", True) or min_qty <= 0 or qty_step <= 0 or tick_size <= 0 or ct_val <= 0:
            raise ValueError(f"invalid instrument info: {info}")
        self._min_qty = min_qty
        self._qty_step = qty_step
        self._tick_size = tick_size
        self._ct_val = ct_val
        self._instrument_info_ready = True
        self.log.info(
            f"[instrument_guard] {self._symbol} minSz={min_qty} lotSz={qty_step} "
            f"tickSz={tick_size} ctVal={ct_val}"
        )

    # ==================== PnL/Fee持久化 ====================

    def _is_reduce_fill(self, side: str, pos_side: str, fill_pnl: float) -> bool:
        side = str(side or "").lower()
        pos_side = str(pos_side or "").lower()
        if side == "sell" and pos_side == "long":
            return True
        if side == "buy" and pos_side == "short":
            return True
        return abs(fill_pnl) > 1e-9

    def _record_fill_close_transaction(self, fill: dict, fee: float, fill_pnl: float):
        try:
            qty = abs(safe_float(fill.get("fillSz", 0)))
            exit_px = safe_float(fill.get("fillPx", 0))
            if qty <= 0 or exit_px <= 0:
                return
            pos_side = str(fill.get("posSide", "") or "").lower()
            side = "long" if pos_side == "long" else "short" if pos_side == "short" else pos_side or "unknown"
            gross = fill_pnl
            net = gross - fee
            if side == "long":
                entry = exit_px - (gross / (qty * max(self._ct_val, 1e-12)))
            elif side == "short":
                entry = exit_px + (gross / (qty * max(self._ct_val, 1e-12)))
            else:
                entry = exit_px
            ts_ms = int(safe_float(fill.get("ts", 0)))
            self._record_tp_transaction(side, max(0.0, entry), exit_px, qty, gross, fee, net, ts_ms=ts_ms)
        except Exception as e:
            self.log.warning(f"[PnLSync] record close transaction failed: {e}")

    def _record_fill_to_database(
        self,
        fill: dict,
        fill_id: str,
        side: str,
        pos_side: str,
        fee: float,
        fill_pnl: float,
        is_reduce: bool,
    ):
        """Persist every exchange fill in the shared idempotent trade table."""
        db = getattr(self, "_db", None)
        if db is None:
            return
        try:
            qty = abs(safe_float(fill.get("fillSz", 0)))
            price = safe_float(fill.get("fillPx", 0))
            if qty <= 0 or price <= 0:
                return
            ts_ms = int(safe_float(fill.get("ts", 0))) or int(time.time() * 1000)
            # Keep the table's pnl field on the same net-of-fee convention as
            # pnl_records*.csv; gross can be reconstructed as pnl + fee.
            net_pnl = fill_pnl - fee if is_reduce else 0.0
            ct_val = max(safe_float(getattr(self, "_ct_val", 1.0)), 1e-12)
            db.add_trade(
                ts=ts_ms,
                strategy="futures_hedge",
                side=str(side or ""),
                symbol=self._symbol,
                price=price,
                quantity=qty,
                cost=abs(qty * price * ct_val),
                pnl=net_pnl,
                fee=fee,
                order_id=str(fill.get("ordId") or ""),
                fill_id=str(fill_id or ""),
                pos_side=str(pos_side or ""),
                reduce_only=bool(is_reduce),
                fill_type="close" if is_reduce else "open",
            )
        except Exception as e:
            self.log.warning(f"[PnLSync] shared trade write failed: {e}")

    def resume(self, operator: str = "manual") -> dict:
        """Require an explicit operator to clear a latched risk stop."""
        operator = str(operator or "manual").strip() or "manual"
        if self._client:
            try:
                if not self._sync_balance():
                    return {"ok": False, "reason": "balance_sync_failed", "halt_reason": self._halt_reason}
                sync_ok = self._sync_exchange_state()
                if not sync_ok and not bool(getattr(self, "_last_position_query_ok", True)):
                    return {"ok": False, "reason": "exchange_reconcile_failed", "halt_reason": self._halt_reason}
                live_orders = self._fetch_live_orders()
                coverage = self._update_exchange_tp_coverage(live_orders)
                for side, pos in (("long", self._long), ("short", self._short)):
                    if safe_float(getattr(pos, "qty", 0.0)) > self._position_qty_tolerance():
                        covered = safe_float(coverage.get(side, 0.0)) if isinstance(coverage, dict) else 0.0
                        if covered + self._position_qty_tolerance() < safe_float(pos.qty):
                            return {"ok": False, "reason": f"tp_coverage_incomplete:{side}", "halt_reason": self._halt_reason}
            except Exception as exc:
                self.log.warning(f"[halt] resume preflight failed: {exc}")
                return {"ok": False, "reason": "exchange_reconcile_failed", "halt_reason": self._halt_reason}
        self._ensure_daily_risk_window()
        positions_flat = (
            safe_float(getattr(self._long, "qty", 0.0)) <= self._position_qty_tolerance()
            and safe_float(getattr(self._short, "qty", 0.0)) <= self._position_qty_tolerance()
        )
        if self._is_hard_stop_active() and not positions_flat:
            return {"ok": False, "reason": "hard_stop_still_active", "halt_reason": self._halt_reason}
        if self._is_hard_stop_active() and positions_flat:
            self._session_start_total_pnl = self._total_pnl
            if self._cached_equity > 0:
                self._risk_equity_baseline = self._cached_equity
                self._risk_equity_baseline_ts = time.time()
        if self._client and self._account_margin_data_block_reason():
            return {"ok": False, "reason": "account_risk_data_unavailable", "halt_reason": self._halt_reason}
        self._daily_stop_latched = False
        self._daily_stop_until = 0.0
        self._daily_stop_reason = ""
        self._day_start_total_pnl = self._total_pnl
        self._day_equity_baseline = self._cached_equity if self._cached_equity > 0 else self._day_equity_baseline
        self._save_pnl_data()
        self._clear_halt(f"resume:{operator}")
        self.log.warning(f"[halt] manual resume accepted operator={operator}")
        return {"ok": True, "operator": operator, "reduce_only": bool(getattr(self, "_reduce_only_mode", False))}

    def _in_recovery_mode(self) -> bool:
        return False

    def _side_value(self, side: str, key: str, default=None):
        """Read a side-specific setting with a legacy shared fallback."""
        from strategies.hedge_calculations import side_value
        return side_value(getattr(self, "_side_config", {}), side, key, default)

    def _side_float(self, side: str, key: str, default: float) -> float:
        from strategies.hedge_calculations import side_float
        return side_float(getattr(self, "_side_config", {}), side, key, default)

    def _side_int(self, side: str, key: str, default: int) -> int:
        from strategies.hedge_calculations import side_int
        return side_int(getattr(self, "_side_config", {}), side, key, default)

    def _effective_base_pos_usdt(self, side: str = "") -> float:
        from strategies.hedge_calculations import effective_base_pos_usdt
        return effective_base_pos_usdt(
            self._base_pos_usdt,
            getattr(self, "_side_config", {}),
            side,
            self._in_recovery_mode(),
            safe_float(getattr(self, "_recovery_pos_multiplier", 1.0)),
            bool(getattr(self, "_margin_first_capacity_enabled", False)),
            self._account_margin_load_ratio(),
            safe_float(getattr(self, "_margin_first_normal_margin_ratio", 1.0)),
            safe_float(getattr(self, "_margin_first_max_margin_ratio", 0.0)),
            safe_float(getattr(self, "_margin_first_reduced_size_ratio", 0.50)),
        )

    def _effective_side_step(self, side: str) -> float:
        from strategies.hedge_calculations import effective_side_step
        base = effective_side_step(
            getattr(self, "_side_config", {}), side, self._add_step
        )
        return atr_adaptive_step(
            base,
            safe_float(getattr(self, "_moca_atr_ratio", 0.0)),
            bool(getattr(self, "_grid_atr_enabled", False)),
            safe_float(getattr(self, "_grid_atr_multiplier", 0.0)),
            safe_float(getattr(self, "_grid_atr_min_step", 0.0008)),
            safe_float(getattr(self, "_grid_atr_max_step", 0.02)),
        )

    def _effective_side_multiplier(self, side: str) -> float:
        from strategies.hedge_calculations import effective_side_multiplier
        return effective_side_multiplier(getattr(self, "_side_config", {}), side, self._add_mult)

    def _effective_side_max_adds(self, side: str) -> int:
        from strategies.hedge_calculations import effective_side_max_adds
        return effective_side_max_adds(getattr(self, "_side_config", {}), side, self._max_adds)

    def _side_entry_trigger_blocked(self, side: str) -> bool:
        """Optional first-entry trigger; existing inventory is never trapped."""
        side = str(side or "").lower()
        if side not in {"long", "short"}:
            return True
        position = self._long if side == "long" else self._short
        if safe_float(getattr(position, "qty", 0.0)) > self._position_qty_tolerance():
            return False
        trigger = self._side_float(side, "entry_trigger_price", 0.0)
        price = safe_float(getattr(self, "_last_price", 0.0))
        if trigger <= 0 or price <= 0:
            return False
        condition = str(self._side_value(side, "entry_trigger_condition", "below" if side == "long" else "above")).lower()
        if condition in {"above", "gte", ">="}:
            return price < trigger
        return price > trigger

    def _effective_add_cooldown(self) -> float:
        from strategies.hedge_calculations import effective_add_cooldown
        return effective_add_cooldown(self._cooldown, str(getattr(self, "_moca_state", "") or ""))

    def _effective_drift_threshold(self) -> float:
        from strategies.hedge_calculations import effective_drift_threshold
        return effective_drift_threshold(self._drift_threshold, str(getattr(self, "_moca_state", "") or ""))

    def _enter_startup_recovery(self, reason: str):
        self._startup_sync_ts = 0.0
        self._startup_recovery_active = False
        self._startup_recovery_ended_at = 0.0

    def _in_startup_recovery(self) -> bool:
        self._startup_recovery_active = False
        return False

    def _dominant_side(self) -> str:
        from strategies.hedge_calculations import dominant_side
        base = dominant_side(self._inventory_or_create, self._min_qty)
        gap = safe_float(getattr(self, "_throttle_dominant_gap_ratio", 0.0))
        if not base or gap <= 0:
            return base
        inv = self._inventory_or_create
        lq = max(0.0, safe_float(getattr(getattr(inv, "long", None), "qty", 0)))
        sq = max(0.0, safe_float(getattr(getattr(inv, "short", None), "qty", 0)))
        gross = max(lq, sq)
        if gross <= 0:
            return base
        if abs(lq - sq) / gross < gap:
            return ""
        return base

    def _in_post_recovery_refill_delay(self) -> bool:
        return False

    def _throttle_dominant_net_budget_released(self, side: str) -> bool:
        """True when the dominant side still has room inside this symbol's own
        net-exposure budget.

        Semantics: the per-symbol net cap (``max_net_notional_soft_usdt``) is
        the authoritative budget for how one-sided a profile may become.
        Account-wide THROTTLED mode is a second, coarser layer and must not
        fire before that budget is spent — otherwise a small profile that is
        legitimately one-sided (RAM at net -238 vs a 300 soft cap) stays
        frozen at account level and can never add the missing leg back, which
        is precisely the deadlock this gate removes.

        Fails closed: no budget configured, or no usable mark price, keeps the
        legacy veto in place.
        """
        if not bool(getattr(self, "_throttle_dominant_net_budget_gate_enabled", False)):
            return False
        soft = safe_float(getattr(self, "_max_net_notional_soft_usdt", 0.0))
        if soft <= 0:
            return False
        px = safe_float(getattr(self, "_last_price", 0.0))
        if px <= 0:
            return False
        net = abs(safe_float(self._net_notional_exposure(px)))
        if net >= soft:
            # 主导边净敞口已耗尽预算 → 封锁该边加仓（实盘趋势风控生效）。
            # 仅在状态切换时记录一次，避免高频调用刷库。
            if getattr(self, "_last_throttle_action", "released") != "blocked":
                self._last_throttle_action = "blocked"
                _record_trend_event("throttle", self._symbol, side=side, action="blocked", net=net, soft=soft)
            return False
        now = time.time()
        if now - safe_float(getattr(self, "_last_throttle_release_log_ts", 0.0)) >= 60.0:
            self._last_throttle_release_log_ts = now
            self.log.info(
                f"[throttle_release] 释放主导边加仓 side={side} "
                f"net={net:.2f}U soft={soft:.2f}U symbol={self._symbol}"
            )
            if getattr(self, "_last_throttle_action", "released") != "released":
                self._last_throttle_action = "released"
                _record_trend_event("throttle", self._symbol, side=side, action="released", net=net, soft=soft)
        return True

    def _portfolio_throttled_dominant_side_blocked(self, side: str) -> bool:
        """In THROTTLED account mode, block only the dominant side.

        HARD account mode remains a full opening veto; weak-side inventory
        repair uses its separate explicit intent path and hard checks.

        With ``throttle_dominant_net_budget_gate_enabled`` the veto is skipped
        while this symbol's own net exposure is still inside its soft cap, so
        a one-sided-but-under-budget profile can keep rebuilding its hedge.
        """
        side = str(side or "").lower()
        if side not in {"long", "short"}:
            return False
        margin_load = self._account_margin_load_ratio()
        mode = account_margin_mode(
            margin_load,
            safe_float(getattr(self, "_account_margin_soft_limit", 0.0)),
            safe_float(getattr(self, "_account_margin_hard_limit", 0.0)),
        )
        if mode != "THROTTLED":
            return False
        dominant = self._dominant_side()
        if dominant != side:
            return False
        if self._throttle_dominant_net_budget_released(side):
            return False
        return True

    def _recent_side_loss_active(self, side: str) -> bool:
        if not bool(getattr(self, "_recent_side_loss_guard_enabled", True)):
            return False
        side = str(side or "").lower()
        if side not in {"long", "short"}:
            return False
        until = 0.0
        if isinstance(getattr(self, "_recent_side_loss_until", None), dict):
            until = safe_float(self._recent_side_loss_until.get(side, 0.0))
        return time.time() < until

    def _net_exposure_side_blocks(self) -> tuple[bool, bool]:
        return net_exposure_side_blocks(self._long.qty, self._short.qty, self._max_net_exposure)

    def _net_notional_exposure(self, price: float = 0.0) -> float:
        px = safe_float(price) or safe_float(getattr(self, "_last_price", 0.0))
        contract_value = max(0.0, safe_float(getattr(self, "_ct_val", 0.0)))
        return abs(
            safe_float(getattr(self._long, "qty", 0.0))
            - safe_float(getattr(self._short, "qty", 0.0))
        ) * px * contract_value

    def _dominant_side_add_block_reason(self, side: str) -> str:
        if side not in {"long", "short"}:
            return "invalid_side"
        if self._dominant_side() != side:
            return ""
        return self._grid_capacity_block_reason(side)

    def _session_recovery_remaining_sec(self) -> float:
        if self._recovery_minutes <= 0:
            return 0.0
        return max(0.0, self._recovery_minutes * 60 - (time.time() - self._session_started_at))

    def set_liquidation_status(self, status: Dict):
        self._liquidation_status = dict(status or {})

    def _record_event(self, event_type: str, side: str = "", reason: str = "", detail: str = "", price: float = 0.0, qty: float = 0.0, dedupe_sec: float = 12.0):
        now = time.time()
        fp = (event_type, side, reason, detail)
        if self._last_event_fp == fp and (now - self._last_event_ts) < dedupe_sec:
            return
        self._last_event_type = event_type
        self._last_event_side = side
        self._last_event_reason = reason
        self._last_event_detail = detail
        self._last_event_price = price or 0.0
        self._last_event_qty = qty or 0.0
        self._last_event_ts = now
        self._last_event_fp = fp

    def _set_add_block(self, side: str = "", reason: str = "", cooldown_left: float = 0.0):
        if side == "long":
            self._long_add_block_reason = reason
            self._long_add_cooldown_left = max(0.0, cooldown_left)
        elif side == "short":
            self._short_add_block_reason = reason
            self._short_add_cooldown_left = max(0.0, cooldown_left)
        else:
            self._global_add_block_reason = reason
            self._global_add_cooldown_left = max(0.0, cooldown_left)

    # ==================== 分段大额预埋平仓单 ====================

    def _add_to_segment(self, side: str, price: float, qty: float, cost: float):
        """将加仓层加入分段池"""
        self._layer_history[side].append(Layer(price=price, qty=qty, cost=cost, ts=time.time()))
        if not self._seg_enable:
            return
        # Group layers into segments based on segment_layer_limit
        segments = self._segments[side]
        if not segments or len(segments[-1].layers) >= self._seg_layer_limit:
            seg = Segment(side=side)
            segments.append(seg)
            self.log.info(f"[分段] {side} 创建新分段 #{len(segments)}")
        seg = segments[-1]
        seg.layers.append(self._layer_history[side][-1])
        seg.total_qty += qty
        prices = [l.price for l in seg.layers]
        seg.min_price = min(prices)
        seg.max_price = max(prices)
        # Set bulk close price
        if side == "long":
            seg.close_price = round((seg.max_price * (1 + self._seg_close_offset)) / 0.0001) * 0.0001
        else:
            seg.close_price = round((seg.min_price * (1 - self._seg_close_offset)) / 0.0001) * 0.0001
        self.log.info(f"[分段] {side}#{len(segments)}: {seg.total_qty:.1f}units range={seg.min_price:.4f}~{seg.max_price:.4f} close@{seg.close_price:.4f}")

    def _refresh_segment_orders(self):
        """刷新所有分段大额平仓单 (每2秒统一执行)"""
        if not self._seg_enable or not self._client or not self._last_price:
            return
        if self._trading_halted:
            return
        now = time.time()
        if now - self._last_seg_refresh < max(0.5, self._seg_refresh_ms / 1000.0):
            return
        self._last_seg_refresh = now
        for side in ["long", "short"]:
            for i, seg in enumerate(self._segments[side]):
                if seg.total_qty <= 0:
                    continue
                # Check if close order needs update
                px = self._last_price
                buffer_px = seg.close_price * self._seg_price_buffer
                near = abs(px - seg.close_price) < buffer_px
                if near:
                    seg.buffer_hit = True
                    continue
                if seg.buffer_hit and abs(px - seg.close_price) >= buffer_px * 2:
                    seg.buffer_hit = False
                if seg.buffer_hit:
                    continue
                # If order already exists at same price, keep it
                if seg.close_order_id:
                    continue
                order_side = "sell" if side == "long" else "buy"
                qty = round(seg.total_qty / self._qty_step) * self._qty_step
                if qty <= 0:
                    continue
                try:
                    r = self._submit_order(self._symbol, order_side, str(qty),
                        "limit", px=str(seg.close_price), reduce_only=True)
                    oid = r.get("ordId", "")
                    if oid:
                        seg.close_order_id = oid
                        self.log.info(f"[分段大单] {side}#{i+1}: {order_side} {qty}@{seg.close_price:.4f} id={oid}")
                except Exception as e:
                    self.log.warning(f"[分段大单] 挂单失败 {side}#{i+1}: {e}")

    def _check_segment_fills(self):
        """检查分段大单是否成交"""
        if not self._seg_enable or not self._client:
            return None
        actions = []
        for side in ["long", "short"]:
            segs = self._segments[side]
            for i in range(len(segs) - 1, -1, -1):
                seg = segs[i]
                if not seg.close_order_id or seg.total_qty <= 0:
                    continue
                try:
                    r = self._client._call(self._client._trade.get_order, instId=self._symbol, ordId=seg.close_order_id)
                    od = (r.get("data") or [{}])[0]
                    state = od.get("state", "")
                    if state == "filled":
                        fill_px = float(od.get("fillPx", seg.close_price))
                        fill_qty = float(od.get("fillSz", seg.total_qty))
                        pos = self._long if side == "long" else self._short
                        contract_value = max(self._ct_val, 1e-12)
                        gross = fill_qty * contract_value * ((fill_px - pos.entry) if side == "long" else (pos.entry - fill_px))
                        fee = fill_qty * contract_value * fill_px * self.MAKER_FEE
                        net = gross - fee
                        if self._client:
                            self._total_fees += fee
                            self._total_pnl += net
                        pos.qty = max(0, pos.qty - fill_qty)
                        pos.cost = pos.cost * (pos.qty / max(1, pos.qty + fill_qty))
                        self.log.info(f"[分段大单成交] {side}#{i+1}: {fill_qty}@{fill_px:.4f} net={net:.2f}U")
                        self._save_pnl_data()
                        # Remove this segment
                        del segs[i]
                except Exception as e:
                    self.log.warning(f"[分段大单] 成交检查失败 {side}#{i+1}: {e}")
        return actions if actions else None

    def _clear_segments(self, side=None):
        self._layer_tp_orders.clear()
        """清空分段 (全平时调用)"""
        if side:
            for seg in self._segments[side]:
                if seg.close_order_id and self._client:
                    try:
                        self._cancel_order(seg.close_order_id)
                    except Exception as e:
                        pass
                        self.log.warning(f"[分段清空] 撤单失败 {seg.close_order_id}: {e}")
            self._segments[side] = []
            self._layer_history[side] = []
        else:
            for s in ["long", "short"]:
                self._clear_segments(s)

    def _maybe_restore_missing_side(self):
        if not self._client or self._trading_halted or self._in_startup_recovery() or self._in_post_recovery_refill_delay():
            return
        if not self._startup_auto_hedge_missing_side:
            return
        long_missing = self._long.qty <= 0 < self._short.qty
        short_missing = self._short.qty <= 0 < self._long.qty
        if not long_missing and not short_missing:
            return
        if self._last_price <= 0:
            return
        missing_side = "long" if long_missing else "short"
        base_pos_usdt = self._effective_base_pos_usdt()
        target_qty = contract_qty_for_margin(
            base_pos_usdt,
            self._last_price,
            self._base_lev,
            self._ct_val,
            self._qty_step,
            self._min_qty,
        )
        if target_qty <= 0:
            return
        self._clear_side_runtime_state(missing_side, reason=f"auto_restore_{missing_side}")
        if missing_side == "long":
            self._long = Pos("long", self._last_price, target_qty, base_pos_usdt, 0)
        else:
            self._short = Pos("short", self._last_price, target_qty, base_pos_usdt, 0)
        self._enter_startup_recovery(f"auto_restore_{missing_side}")
        order_side = "buy" if missing_side == "long" else "sell"
        try:
            result = self._place_open_order(order_side, target_qty, missing_side)
            ord_id = result.get("ordId", "") if isinstance(result, dict) else ""
            if ord_id:
                self.log.warning(f"[sync] auto restore {missing_side} qty={target_qty} ordId={ord_id}")
            else:
                self.log.warning(f"[sync] auto restore {missing_side} failed: {result}")
        except Exception as e:
            self.log.warning(f"[sync] auto restore {missing_side} failed: {e}")

    # ==================== 余额实盘同步 ====================

    def _pending_open_qty(self, side: str):
        """Return live non-reduce-only quantity for a side, or None if unavailable."""
        from strategies.hedge_grid import pending_open_qty
        return pending_open_qty(
            side,
            self._fetch_live_orders,
            lambda msg: self.log.warning(msg),
        )

    @property
    def _long(self):
        return self._inventory_or_create.long

    @_long.setter
    def _long(self, value):
        self._inventory_or_create.long = value

    @property
    def _short(self):
        return self._inventory_or_create.short

    @_short.setter
    def _short(self, value):
        self._inventory_or_create.short = value

    @property
    def _cached_avail(self):
        return self._account_state_or_create.cached_avail

    @_cached_avail.setter
    def _cached_avail(self, value):
        self._account_state_or_create.cached_avail = safe_float(value)

    @property
    def _cached_frozen(self):
        return self._account_state_or_create.cached_frozen

    @_cached_frozen.setter
    def _cached_frozen(self, value):
        self._account_state_or_create.cached_frozen = safe_float(value)

    @property
    def _cached_equity(self):
        return self._account_state_or_create.cached_equity

    @_cached_equity.setter
    def _cached_equity(self, value):
        self._account_state_or_create.cached_equity = safe_float(value)

    def _get_candle_id(self) -> str:
        if not self._candle_anti_dup or not self._client: return ""
        try:
            k = self._client.get_candles(self._symbol, bar="5m", limit=1)
            if k:
                first = k[0]
                if isinstance(first, dict):
                    ts = first.get("ts")
                    return str(ts) if ts is not None else ""
                if isinstance(first, (list, tuple)) and first:
                    return str(first[0])
        except Exception as e:
            self.log.warning(f"[candle_id] read failed: {e}")
        return ""

    def _check_floating(self, price: float) -> bool:
        if self._long.qty <= 0 and self._short.qty <= 0:
            self._floating_loss_ratio = 1.0
            self._floating_loss_freeze_active = False
            return True
        tc = self._long.cost + self._short.cost
        if tc <= 0: return True
        contract_value = max(self._ct_val, 0.0)
        lv = self._long.qty * price * contract_value
        sv = self._short.qty * price * contract_value
        lp = self._long.qty * self._long.entry * contract_value
        sp = self._short.qty * self._short.entry * contract_value
        fp = ((lv - lp) + (sp - sv)) / max(tc, self._base_pos_usdt * 0.5)
        if fp < -self._floating_loss_freeze:
            if fp < -10.0:
                self.log.info(f"[风控] 浮亏异常({fp:.1%}), 跳过冻结")
                self._floating_loss_freeze_active = False
                return True
            # Do not abort the complete tick. A global return here also blocks
            # weak-side inventory repair, which is the safety action needed
            # when the hedge is imbalanced. Dominant-side capacity is blocked
            # by _grid_capacity_block_reason and add size is reduced.
            self._floating_loss_ratio = 0.25
            self._floating_loss_freeze_active = True
            self.log.warning(
                f"[风控] {self._symbol} 浮亏超限({fp:.1%})，主导侧暂停扩张，弱侧允许修复"
            )
            return True
        self._floating_loss_freeze_active = False
        if fp < -self._floating_loss_shrink:
            self._floating_loss_ratio = 0.5
        else:
            self._floating_loss_ratio = 1.0
        return True

    def set_reduce_only(self, enabled: bool):
        enabled = bool(enabled)
        changed = enabled != getattr(self, "_reduce_only_mode", False)
        self._reduce_only_mode = enabled
        if enabled:
            self._last_reduce_only_cancel = time.time()
            self._cancel_opening_orders()
        if changed:
            self.log.warning(f"[reduce_only] {self._symbol} enabled={enabled}")

    def _flatten_positions(self, reason: str):
        if not self._client:
            return
        now = time.time()
        if now - self._last_flatten_attempt < self._flatten_cooldown:
            return
        self._last_flatten_attempt = now
        self._cancel_all_live_orders()
        close_attempts = []
        if self._long.qty > 0:
            qty = max(round(self._long.qty / self._qty_step) * self._qty_step, self._min_qty)
            close_attempts.append(("sell", "long", qty))
        if self._short.qty > 0:
            qty = max(round(self._short.qty / self._qty_step) * self._qty_step, self._min_qty)
            close_attempts.append(("buy", "short", qty))
        for side, pos_side, qty in close_attempts:
            try:
                result = self._submit_order(
                    self._symbol,
                    side,
                    str(qty),
                    ord_type="market",
                    pos_side=pos_side,
                    reduce_only=True,
                )
                if result.get("ordId"):
                    if pos_side == "long":
                        self._long = Pos("long")
                    else:
                        self._short = Pos("short")
                    self.log.warning(f"[停机保护] {reason}: 市价平{pos_side} {qty}张 ordId={result.get('ordId')}")
                else:
                    s_code = result.get("sCode", "") if isinstance(result, dict) else ""
                    if s_code == "51169":
                        if pos_side == "long":
                            self._long = Pos("long")
                        else:
                            self._short = Pos("short")
                    self.log.warning(f"[停机保护] {reason}: 市价平{pos_side}返回异常 {result}")
            except Exception as e:
                self.log.error(f"[停机保护] {reason}: 市价平{pos_side}失败: {e}")
    # ==================== 主入口 ====================

    def on_tick(self, ticker: dict) -> Optional[dict]:
        if not self._running or not self._enabled: return None
        if time.time() < self._breaker_until: return None
        price = safe_float(ticker.get("last", 0))
        if price <= 0: return None
        self._last_price = price
        if getattr(self, "_reduce_only_mode", False) and time.time() - getattr(self, "_last_reduce_only_cancel", 0.0) >= 60:
            self._last_reduce_only_cancel = time.time()
            self._cancel_opening_orders()
        self._current_candle_id = self._get_candle_id()
        if not self._sync_balance():
            # Account state is a prerequisite for every opening decision. A
            # failed refresh must not let stale equity or margin data reach the
            # grid and repair paths on this tick.
            self.log.warning(f"[risk] {self._symbol} account sync failed; skip tick")
            return None
        # Set the daily baseline after refreshing equity but before importing
        # any fill from the new day, so the first close is not absorbed into it.
        self._ensure_daily_risk_window()
        self._sync_recent_fills()
        # Risk halts remain latched across ticks/restarts; only resume() may
        # clear them after fresh account and position checks.
        if self._is_hard_stop_active():
            self._engage_halt("hard_stop")
            if not self._init_done:
                self._sync_exchange_state()
            self._sync_positions()
            self._flatten_positions("hard_stop")
            return None
        self._check_circuit_breaker(price)
        if not self._init_done:
            if self._sync_exchange_state(): return None
            if not bool(getattr(self, "_last_position_query_ok", True)):
                return None
            retry = getattr(self, "_sync_retry_cnt", 0) + 1
            self._sync_retry_cnt = retry
            if retry < 3:
                return None
            self._sync_retry_cnt = 0
            self.log.info(f"[初始化] 尝试{retry}次后仍未检测到持仓, 执行初始化开仓")
            return self._init_pos(price)
        self._sync_positions()
        if self._trading_halted:
            self._flatten_positions(self._halt_reason or "halted")
            return None
        repair = self._maybe_repair_normal_close_missing_side(price)
        if repair is not None:
            return repair
        hard_hedge = self._maybe_repair_hard_net_exposure(price)
        if hard_hedge is not None:
            return hard_hedge
        self._calibrate_layers()  # Opbot层数校准
        # self._ensure_tp_orders()
        self._check_funding_arb()  # 资金费套利
        self._revert_funding_arb()
        self._sync_orders()
        if time.time() - getattr(self, "_last_tp_ensure", 0.0) >= 60:
            self._last_tp_ensure = time.time()
            self._init_existing_tp(cancel_existing=False)
        over_cap = self._maybe_over_cap_unwind(price)
        if over_cap is not None:
            return over_cap
        pnl_rate = self._calc_pnl(price)
        if self._hard_stop_loss(pnl_rate, price):
            self._engage_halt("hard_stop_loss")
            self._flatten_positions("hard_stop_loss")
            return None
        if not self._check_floating(price): return None
        if pnl_rate > self._peak_pnl: self._peak_pnl = pnl_rate
        action = self._check_tp(pnl_rate, price)
        if action: return action
        self._cleanup_stale_tps(price)
        self._rehang_far_tps(price)
        self._heal_missing_tp_coverage()
        self._check_trend_orders()
        self._refresh_segment_orders()
        self._check_segment_fills()
        if getattr(self, "_balance_tick", 0) % 10 == 0:
            self._log_balance()
            dact = self._check_drift()
            if dact: return dact
        self._balance_tick = getattr(self, "_balance_tick", 0) + 1
        action = self._check_layered_tp(price)
        if action: return action
        self._maybe_flush_pnl_heartbeat()
        return self._check_add(price)

    def _init_pos(self, price: float) -> dict:
        if self._trading_halted or self._is_hard_stop_active():
            self.log.warning("[INIT] halted, skip new position init")
            return {"type":"skip","reason":"trading_halted"}
        if getattr(self, "_reduce_only_mode", False):
            return {"type":"skip","reason":"reduce_only_mode"}
        if self._daily_stop():
            self.log.warning("[INIT] daily stop active, skip new position init")
            return {"type":"skip","reason":"daily_stop"}
        # Double-check: if positions already exist on exchange, skip init
        has_pos = None
        if self._client:
            try:
                rows = self._fetch_position_rows()
                has_pos = False
                for p in rows:
                    if abs(safe_float(p.get("pos", 0))) > self._position_qty_tolerance():
                        has_pos = True
                        break
                if has_pos:
                    self._sync_exchange_state()
                    self.log.info("[初始化] 检测到已有持仓,转入恢复接管")
                    return {"type":"skip","reason":"positions_exist"}
            except Exception as e:
                self.log.warning(f"[初始化] 已有持仓检测失败: {e}")
        if self._client and has_pos is None:
            return {"type":"skip","reason":"position_state_unavailable"}
        if self._client:
            try:
                self._set_leverage(self._symbol, self._base_lev, self._margin_mode)
            except Exception as exc:
                self.log.warning(f"[INIT] leverage unavailable; skip opening: {exc}")
                self._set_add_block(reason="leverage_unavailable")
                return {"type": "skip", "reason": "leverage_unavailable"}
        long_base_usdt = self._effective_base_pos_usdt("long")
        short_base_usdt = self._effective_base_pos_usdt("short")
        base_pos_usdt = max(long_base_usdt, short_base_usdt)
        qty_long = contract_qty_for_margin(
            long_base_usdt, price, self._base_lev, self._ct_val, self._qty_step, self._min_qty
        )
        qty_short = contract_qty_for_margin(
            short_base_usdt, price, self._base_lev, self._ct_val, self._qty_step, self._min_qty
        )
        self._long = Pos("long", price, qty_long, long_base_usdt, 0)
        self._short = Pos("short", price, qty_short, short_base_usdt, 0)
        self._init_done = True; self._cur_lev = self._base_lev
        self._last_add_candle = self._current_candle_id
        self._layer_tps[str(self._next_layer_id)] = price * (1 + self._effective_side_tp_ratio("long", "first"))
        self._next_layer_id += 1
        self._layer_tps[str(self._next_layer_id)] = price * (1 - self._effective_side_tp_ratio("short", "first"))
        self._next_layer_id += 1
        self._init_existing_tp()
        self.log.info(f"[对冲] 底仓: {price:.2f} long_qty={qty_long:.4f} short_qty={qty_short:.4f} lev={self._cur_lev}x")
        if self._client:
            open_fee = (qty_long + qty_short) * price * self._ct_val * self.TAKER_FEE
            self._total_fees += open_fee
            self.log.info(f"[费用] 开仓手续费: {open_fee:.4f}U")
        actions = []
        if self._client:
            actions.append({"action":"buy","quantity":str(qty_long),"pos_side":"long","cost":str(long_base_usdt)})
            actions.append({"action":"sell","quantity":str(qty_short),"pos_side":"short","cost":str(short_base_usdt)})
            init_tp_ratio_l = self._effective_tp_ratio(self._effective_side_tp_ratio("long", "first"), "maker")
            init_tp_ratio_s = self._effective_tp_ratio(self._effective_side_tp_ratio("short", "first"), "maker")
            tp_px_l = round(price * (1 + init_tp_ratio_l) / 0.0001) * 0.0001
            tp_px_s = round(price * (1 - init_tp_ratio_s) / 0.0001) * 0.0001
            close_half_l = round(qty_long * 0.5 / self._qty_step) * self._qty_step
            close_half_s = round(qty_short * 0.5 / self._qty_step) * self._qty_step
            if close_half_l >= self._min_qty:
                actions.append({"action":"sell","quantity":str(close_half_l),"pos_side":"long","order_type":"limit","price":str(tp_px_l),"reduce_only":True})
            if close_half_s >= self._min_qty:
                actions.append({"action":"buy","quantity":str(close_half_s),"pos_side":"short","order_type":"limit","price":str(tp_px_s),"reduce_only":True})
            self._grid_center_price = price
            gpx_l = round(price * (1 - self._effective_side_step("long")) / 0.0001) * 0.0001
            gpx_s = round(price * (1 + self._effective_side_step("short")) / 0.0001) * 0.0001
            gq_l = contract_qty_for_margin(
                long_base_usdt, gpx_l, self._cur_lev, self._ct_val, self._qty_step, self._min_qty
            )
            gq_s = contract_qty_for_margin(
                short_base_usdt, gpx_s, self._cur_lev, self._ct_val, self._qty_step, self._min_qty
            )
            actions.append({"action":"buy","quantity":str(gq_l),"pos_side":"long","order_type":"limit","price":str(gpx_l)})
            self._long_grid_levels.append(gpx_l)
            actions.append({"action":"sell","quantity":str(gq_s),"pos_side":"short","order_type":"limit","price":str(gpx_s)})
            self._short_grid_levels.append(gpx_s)
            self.log.info(f"Grid: {gq_l}@{gpx_l:.4f} / {gq_s}@{gpx_s:.4f}")
        self._save_pnl_data()
        return {"type":"init","actions":actions,"price":price,"cost":str(base_pos_usdt * 2)}

    def _available_after_reserve(self) -> float:
        # Some recovery/test instances are constructed without full __init__;
        # treat a missing reserve as zero rather than disabling the guard path.
        reserve = safe_float(getattr(self, "_reserved_usdt", 0.0))
        return max(0.0, self._cached_avail - reserve)

    def _mgn_ratio_opening_block_reason(self) -> str:
        from strategies.hedge_risk import mgn_ratio_opening_block_reason
        return mgn_ratio_opening_block_reason(
            self._account_margin_data_block_reason(),
            self._account_mgn_ratio(),
            safe_float(getattr(self, "_mgn_ratio_open_limit", 5.0)),
            safe_float(getattr(self, "_mgn_ratio_emergency_limit", 2.0)),
            safe_float(getattr(self, "_mgn_ratio_pause_limit", 3.0)),
        )

    def _can_spend_usdt(self, usdt: float) -> bool:
        return usdt <= self._available_after_reserve() * 0.98

    def _can_open_new_position(self) -> bool:
        min_need = max(self._min_qty, self._effective_base_pos_usdt() * 0.5)
        return self._available_after_reserve() >= min_need

    def _effective_max_side_notional_usdt(self, side: str = "") -> float:
        from strategies.hedge_calculations import effective_max_side_notional_usdt
        return effective_max_side_notional_usdt(self._dynamic_notional_limits(), side)

    def _dynamic_notional_limits(self) -> dict:
        price = safe_float(getattr(self, "_last_price", 0.0))
        ct_val = max(0.0, safe_float(getattr(self, "_ct_val", 1.0)))
        raw = self._account_risk_snapshot if isinstance(self._account_risk_snapshot, dict) else {}
        usdt = raw.get("usdt") if isinstance(raw.get("usdt"), dict) else {}
        margin_load = self._account_margin_load_ratio()
        long_notional = self._long.qty * price * ct_val
        short_notional = self._short.qty * price * ct_val
        dynamic_margin_limit = safe_float(
            getattr(self, "_dynamic_side_notional_max_margin_ratio", 0.35)
        )
        # The dynamic cap is an account-level soft limiter. Keep it active for
        # an established hedge even when gross frozen margin crosses its base
        # threshold; directional risk is governed by net exposure in this case.
        if self._long.qty > 0 and self._short.qty > 0:
            hedge_ratio = net_exposure_ratio(long_notional, short_notional)
            dynamic_margin_limit = net_aware_margin_stop_ratio(
                dynamic_margin_limit,
                hedge_ratio,
                max(
                    dynamic_margin_limit,
                    safe_float(getattr(self, "_max_margin_ratio", dynamic_margin_limit)),
                    # The per-symbol max_margin_ratio is a sizing target, not
                    # the account soft risk line. A balanced hedge may use the
                    # configured account soft limit while net exposure remains
                    # bounded by the independent notional guards.
                    safe_float(
                        getattr(
                            self,
                            "_account_margin_soft_limit",
                            dynamic_margin_limit,
                        )
                    ),
                ),
                safe_float(getattr(self, "_inventory_margin_hedge_credit_ratio", 0.20)),
            )
        dynamic_enabled = (
            bool(getattr(self, "_dynamic_side_notional_enabled", True))
            and margin_load < dynamic_margin_limit
        )
        return dynamic_notional_limits(
            self._max_side_notional_usdt,
            self._cached_equity,
            margin_load,
            usdt.get("mgnRatio"),
            long_notional,
            short_notional,
            dynamic_enabled,
            safe_float(getattr(self, "_dynamic_side_notional_max_multiplier", 1.4)),
            available_margin=self._available_after_reserve(),
            leverage=max(1.0, safe_float(getattr(self, "_base_lev", 1.0))),
            equity_ratio=safe_float(getattr(self, "_dynamic_side_notional_equity_ratio", 0.0)),
            margin_budget_utilization=safe_float(
                getattr(self, "_dynamic_side_notional_margin_budget_ratio", 0.80)
            ),
            base_long_cap=(
                safe_float(getattr(self, "_max_long_side_notional_usdt", 0.0))
                if safe_float(getattr(self, "_max_long_side_notional_usdt", 0.0)) > 0 else None
            ),
            base_short_cap=(
                safe_float(getattr(self, "_max_short_side_notional_usdt", 0.0))
                if safe_float(getattr(self, "_max_short_side_notional_usdt", 0.0)) > 0 else None
            ),
            base_total_cap=(
                safe_float(getattr(self, "_max_total_notional_usdt", 0.0))
                if safe_float(getattr(self, "_max_total_notional_usdt", 0.0)) > 0 else None
            ),
        )

    def _opening_order_block_reason(
        self,
        side: str,
        qty: float,
        price: float = 0.0,
        pending_orders=None,
        paired_net_neutral: bool = False,
        inventory_repair: bool = False,
        allow_hard_total_cap: bool = False,
    ) -> str:
        from strategies.hedge_risk import OpeningRiskContext, opening_order_block_reason
        ctx = OpeningRiskContext(
            side=side,
            qty=qty,
            price=price,
            pending_orders=pending_orders,
            paired_net_neutral=paired_net_neutral,
            inventory_repair=inventory_repair,
            allow_hard_total_cap=allow_hard_total_cap,
            reduce_only_mode=bool(getattr(self, "_reduce_only_mode", False)),
            risk_opening_pause_active=self._risk_opening_pause_active(),
            risk_opening_pause_reason=str(getattr(self, "_risk_opening_pause_reason", "") or ""),
            instrument_info_ready=bool(getattr(self, "_instrument_info_ready", False)),
            has_leverage_mismatch=self._has_leverage_mismatch(),
            client_present=bool(getattr(self, "_client", None)) if pending_orders is None else False,
            fetch_live_orders=self._fetch_live_orders,
            tp_coverage_block_reason=self._tp_coverage_block_reason,
            last_price=safe_float(getattr(self, "_last_price", 0.0)),
            ct_val=safe_float(getattr(self, "_ct_val", 0.0)),
            account_budget_allocator_present=getattr(self, "_account_budget_allocator", None) is not None,
            account_budget_block_reason=self._account_budget_block_reason,
            pending_opening_margin=self._pending_opening_margin,
            long_qty=self._long.qty,
            short_qty=self._short.qty,
            dynamic_notional_limits=self._dynamic_notional_limits,
            margin_first_capacity_ready=self._margin_first_capacity_ready,
            account_margin_load_ratio=self._account_margin_load_ratio,
            cached_equity=safe_float(getattr(self, "_cached_equity", 0.0)),
            working_total_notional_ratio=safe_float(getattr(self, "_working_total_notional_ratio", 1.0)),
            max_net_notional_usdt=safe_float(getattr(self, "_max_net_notional_usdt", 0.0)),
            inventory_repair_side_cap_multiplier=safe_float(
                getattr(self, "_inventory_repair_side_cap_multiplier", 1.0)
            ),
            inventory_repair_max_margin_ratio=safe_float(
                getattr(self, "_inventory_repair_max_margin_ratio", 0.35)
            ),
        )
        return opening_order_block_reason(ctx)

    def _order_error_code(self, result) -> str:
        from strategies.hedge_execution import order_error_code
        return order_error_code(result)

    def _extract_order_id(self, result: dict) -> str:
        from strategies.hedge_execution import extract_order_id
        return extract_order_id(result)

    def _order_succeeded(self, result: dict) -> bool:
        from strategies.hedge_execution import order_succeeded
        return order_succeeded(result)

    def _trend_counter_side(self) -> str:
        state = getattr(self, "_moca_state", "")
        if state == "trending_up":
            return "short"
        if state == "trending_down":
            return "long"
        return ""

    def _skew_side_multiplier(self, side: str) -> float:
        dominant = self._dominant_side()
        imbalance = self._net_imbalance_ratio()
        if not dominant or imbalance < self._grid_bias_ratio * 0.5:
            return 1.0
        intensity = min(1.0, max(0.0, (imbalance - self._grid_bias_ratio * 0.5) / max(self._grid_bias_ratio, 0.0001)))
        if side == dominant:
            return max(0.25, 1.0 - self._skew_reduce_strength * intensity)
        return min(1.75, 1.0 + self._skew_boost_strength * intensity)

    def _trend_bias_multiplier(self, side: str) -> float:
        if self._grid_pause_active(side):
            return 0.0
        # Trend-state bias (trending_up -> favor long / trending_down -> favor short)
        # is intentionally removed: it encodes trend-following, which contradicts this
        # strategy's mean-reversion / hedged-grid philosophy.  MOCA remains observe-only
        # (moca.apply_live=false), so `_moca_state` is always "" here regardless.
        return self._skew_side_multiplier(side) * self._inventory_age_multiplier(side)

    def _has_expected_edge(self, side: str, price: float, qty: float, tp_ratio: float, order_mode: str) -> tuple:
        notional = max(price * qty * max(self._ct_val, 0.0), 0.0)
        if notional <= 0:
            return False, "zero_notional"
        expected_gross = notional * tp_ratio
        if order_mode == "taker":
            expected_fee = notional * (self.TAKER_FEE + self.MAKER_FEE)
        else:
            expected_fee = notional * (self.MAKER_FEE * 2)
        state = getattr(self, "_moca_state", "")
        slippage = notional * (0.0002 if state in {"trending_up", "trending_down"} else 0.0003)
        margin_first_edge = (
            self._margin_first_capacity_ready()
            and bool(getattr(self, "_margin_first_relax_edge_enabled", False))
        )
        # FART's small grid orders have their fees and conservative slippage
        # modeled above.  Under margin-first mode, require that modeled net
        # profit to clear the configured cash floor, but do not apply the
        # portfolio-sized percentage floor or state multipliers a second time.
        # All account margin, net-exposure and exchange checks remain upstream.
        floor = max(0.0, self._min_expected_net_usdt)
        if not margin_first_edge:
            floor = max(floor, notional * 0.0015)
            if state in {"trending_up", "trending_down"}:
                floor *= 1.45
            elif state == "spike":
                floor *= 1.8
            if self._recent_side_loss_active(side):
                floor *= 2.0
        margin_load = 0.0
        if self._cached_equity > 0:
            margin_load = self._account_margin_load_ratio()
        # Account-level high-margin edge tightening should not idle tiny independent
        # profiles whose own exposure is negligible. DRAM can consume account margin,
        # but FARTCOIN-sized probes should still trade if their own base edge clears
        # the normal floor. This bypasses only the soft edge-floor multiplier; hard
        # position/margin/available-balance checks still run before any order.
        if not margin_first_edge and not self._small_strategy_margin_bypass(
            side, notional / max(self._cur_lev, 1)
        ):
            # Account budget already targets a 45% margin load. Tightening the
            # edge floor below that target rejects otherwise profitable small
            # grid orders while substantial free margin remains. Use the
            # account target as the first tightening point, then tighten again
            # only at materially higher utilization.
            budget_target = safe_float(
                getattr(getattr(self, "_account_budget_allocator", None), "target_margin_ratio", 0.45)
            )
            tighten_start = max(0.35, min(0.80, budget_target))
            if margin_load >= tighten_start:
                floor *= 1.2
            if margin_load >= min(0.80, tighten_start + 0.15):
                floor *= 1.35
        expected_net = expected_gross - expected_fee - slippage
        if expected_net < floor:
            return False, f"edge_too_low:{expected_net:.4f}"
        return True, ""

    # ==================== 止盈 ====================

    def _close_all(self, reason, pnl_rate, price):
        contract_value = max(self._ct_val, 1e-12)
        long_upnl = (price - self._long.entry) * self._long.qty * contract_value if self._long.qty > 0 else 0
        short_upnl = (self._short.entry - price) * self._short.qty * contract_value if self._short.qty > 0 else 0
        gross = long_upnl + short_upnl
        # 只在有API客户端(实盘模式)时跟踪PnL
        fee = 0.0; net = 0.0
        long_qty = self._long.qty
        short_qty = self._short.qty
        if self._client:
            fee = (long_qty * price + short_qty * price) * contract_value * self.TAKER_FEE if (long_qty > 0 or short_qty > 0) else 0
            net = gross - fee
            self._total_fees += fee; self._total_pnl += net
            self._cancel_all_live_orders()
            if long_qty > 0:
                try:
                    self._submit_order(self._symbol, "sell", str(long_qty), ord_type="market", pos_side="long", reduce_only=True)
                except Exception as e:
                    self.log.error(f"[全平] 平多失败: {e}")
            if short_qty > 0:
                try:
                    self._submit_order(self._symbol, "buy", str(short_qty), ord_type="market", pos_side="short", reduce_only=True)
                except Exception as e:
                    self.log.error(f"[全平] 平空失败: {e}")
        self.log.info(f"[全平] {reason} gross={gross:.2f}U fee={fee:.4f}U net={net:.2f}U total_pnl={self._total_pnl:.2f}U" + (" (模拟)" if not self._client else ""))
        self._init_done = False; self._first_tp_done = False; self._peak_pnl = 0.0
        self._long = Pos("long"); self._short = Pos("short"); self._consecutive_errors = 0
        self._cancel_all_tp_orders()
        self._clear_segments()
        self._save_pnl_data()
        return {"type":"close_all","reason":reason,"pnl":round(net,4),"price":price}

    def _update_order_tracker(self, orders):
        tracker = getattr(self, "_order_tracker", None)
        if not tracker:
            return
        try:
            stats = tracker.update_live_orders(orders or [])
            now = time.time()
            important = stats.get("lost_new") or stats.get("restored") or stats.get("forgotten") or stats.get("changed")
            periodic = now - getattr(self, "_last_order_tracker_log", 0.0) >= 300
            if important or periodic:
                self._last_order_tracker_log = now
                self.log.info(
                    "[订单跟踪] active={} cached={} lost={} added={} restored={} forgotten={}".format(
                        stats.get("active", 0), stats.get("cached", 0), stats.get("lost", 0),
                        stats.get("added", 0), stats.get("restored", 0), stats.get("forgotten", 0)
                    )
                )
        except Exception as e:
            self.log.warning(f"[订单跟踪] update failed: {e}")

    def _update_position_age(self, side: str, qty: float):
        if side not in {"long", "short"}:
            return
        if not isinstance(getattr(self, "_position_first_seen", None), dict):
            self._position_first_seen = {"long": 0.0, "short": 0.0}
        changed = False
        if qty > 0:
            if self._position_first_seen.get(side, 0.0) <= 0:
                self._position_first_seen[side] = time.time()
                changed = True
        else:
            if self._position_first_seen.get(side, 0.0) > 0:
                self._position_first_seen[side] = 0.0
                changed = True
        if changed:
            try:
                self._save_pnl_data()
            except Exception:
                pass

    def _position_age_sec(self, side: str) -> float:
        from strategies.hedge_calculations import position_age_sec
        return position_age_sec(getattr(self, "_position_first_seen", None), side)

    def _is_weak_side(self, side: str) -> bool:
        dominant = self._dominant_side()
        if side not in {"long", "short"} or dominant not in {"long", "short"}:
            return False
        if side == dominant:
            return False
        return self._net_imbalance_ratio() >= max(0.05, min(self._weak_side_margin_imbalance_threshold, 0.95))

    def _strategy_notional_or_cost_ratio(self) -> float:
        if self._cached_equity <= 0:
            return 0.0
        return max(0.0, self._strategy_margin_used() / self._cached_equity)

    def _strategy_cost_ratio(self) -> float:
        return self._strategy_notional_or_cost_ratio()

    @staticmethod
    def _add_rate_limit_bucket(reason_code: str = "") -> str:
        if reason_code == "inventory_repair":
            return "inventory_repair"
        if reason_code in {"paired_weak", "paired_dominant"}:
            return "paired_add"
        return "regular"

    def _add_rate_limit_budget(self, bucket: str) -> float:
        if bucket == "inventory_repair":
            return safe_float(getattr(self, "_inventory_repair_rate_limit_margin_usdt", 45.0))
        if bucket == "paired_add":
            return safe_float(getattr(self, "_paired_add_rate_limit_margin_usdt", 45.0))
        return safe_float(getattr(self, "_regular_add_rate_limit_margin_usdt", 15.0))

    def _check_rate_limit(self, side, margin_needed, max_margin=None, window=None, reason_code=""):
        now = time.time()
        if window is None:
            window = safe_float(getattr(self, "_add_rate_limit_window_sec", 300.0))
        bucket = self._add_rate_limit_bucket(reason_code)
        if max_margin is None:
            max_margin = self._add_rate_limit_budget(bucket)
        if max_margin <= 0 or window <= 0:
            return True
        self._order_records = [r for r in self._order_records if now - r[0] < window]
        side_margin = sum(
            r[2]
            for r in self._order_records
            if r[1] == side and (r[3] if len(r) > 3 else "regular") == bucket
        )
        return side_margin + margin_needed <= max_margin

    def _validate_order(self, side, add_usdt=None, reason_code=""):
        self._sync_balance()
        need = add_usdt or self._effective_base_pos_usdt(side)
        if not self._check_rate_limit(side, need, reason_code=reason_code): return False, "rate limit"
        if self._daily_stop(): return False, "daily stop"
        if not self._check_margin(side, add_usdt): return False, "margin"
        free_avail = self._available_after_reserve()
        if free_avail > 0 and need * 1.02 > free_avail:
            return False, f"insufficient_free_margin need={need*1.02:.1f} free={free_avail:.1f} reserve={self._reserved_usdt:.1f}"
        return True, ""

    def _maybe_over_cap_unwind(self, price: float):
        """Reduce an established CRV inventory that is already over capacity.

        Opening guards correctly reject further gross exposure, but they cannot
        resolve a position created under an older limit.  This path is therefore
        intentionally separate from ``_do_add`` and only ever submits a bounded
        reduce-only market order.  The exchange position is refreshed before the
        next attempt so a stale local quantity cannot cause a 51169 retry loop.
        """
        from strategies.hedge_inventory_repair import maybe_over_cap_unwind
        return maybe_over_cap_unwind(self, price)

    def _unwind_failed_pair_leg(self, side: str, before_qty: float, order_id: str = "") -> dict:
        """Cancel and reconcile the first leg when the paired leg fails."""
        if order_id:
            self._cancel_order(order_id, self._symbol)
        if not self._client:
            return {"confirmed": True, "unwound_qty": 0.0}
        refreshed = self._refresh_position_after_reduce(side)
        if not refreshed.get("confirmed"):
            return {"confirmed": False, "reason": "pair_leg_state_unavailable"}
        actual_qty = max(0.0, safe_float(refreshed.get("qty")))
        filled_qty = max(0.0, actual_qty - max(0.0, safe_float(before_qty)))
        if filled_qty <= self._position_qty_tolerance():
            return {"confirmed": True, "unwound_qty": 0.0}
        close_side = "sell" if side == "long" else "buy"
        result = self._submit_order(
            self._symbol,
            close_side,
            str(round(filled_qty / self._qty_step) * self._qty_step),
            ord_type="market",
            pos_side=side,
            reduce_only=True,
        )
        if not self._order_succeeded(result):
            return {
                "confirmed": False,
                "reason": "pair_leg_unwind_failed",
                "quantity": filled_qty,
            }
        final = self._refresh_position_after_reduce(side)
        return {
            "confirmed": bool(final.get("confirmed")),
            "unwound_qty": filled_qty,
            "remaining_qty": safe_float(final.get("qty")),
        }

    def _check_add(self, price: float) -> Optional[dict]:
        """Opbot-style grid center trigger check"""
        if self._trading_halted: return None
        # Block reasons describe the current tick only; do not expose a stale
        # reason after the order state or market trigger has changed.
        self._set_add_block("long", "")
        self._set_add_block("short", "")
        self._set_add_block(reason="")
        if getattr(self, "_reduce_only_mode", False):
            self._set_add_block(reason="reduce_only_mode")
            return None
        if self._in_startup_recovery():
            self._set_add_block(reason="startup_recovery")
            return None
        if self._in_post_recovery_refill_delay():
            self._set_add_block(reason="post_recovery_refill_delay")
            return None
        dominant_side = self._dominant_side()
        if self._paired_add_triggered(dominant_side, price):
            pair_side = "long" if dominant_side == "long" else "short"
            if self._paired_add_wick_ok(pair_side, price):
                paired = self._do_paired_add(dominant_side, price)
                if paired is not None and paired.get("type") != "skip":
                    return paired
        repair_result = self._maybe_active_inventory_repair(price)
        if repair_result is not None:
            return repair_result
        # Do not globally stop both sides on an inventory-margin pause.
        # Side-level capacity checks below block the dominant side while the
        # weak side remains eligible for bounded net-reducing repair.
        if self._in_recovery_mode() and self._cached_equity > 0:
            margin_load = self._account_margin_load_ratio()
            if margin_load >= self._max_margin_ratio:
                self._set_add_block(reason="recovery_margin_guard")
                return None
        # MOCA/trend side filters are treated as soft bias for two-sided activity.
        # Hard blockers remain margin, position, cooldown, wick and exchange rejects.
        add_cooldown = self._effective_add_cooldown()
        cooldown_left = max(0.0, add_cooldown - (time.time() - self._last_add))
        if cooldown_left > 0:
            self._set_add_block(reason="global_cooldown", cooldown_left=cooldown_left)
            return None
        if self._candle_anti_dup and self._current_candle_id and self._current_candle_id == self._last_add_candle:
            self._set_add_block(reason="same_candle")
            return None
        # Wick filter
        if self._grid_center_price > 0 and not self._wick_check("long" if price < self._grid_center_price else "short", price):
            self._set_add_block(reason="wick_block")
            return None
        # Grid center must be set
        if self._grid_center_price <= 0:
            self._grid_center_price = price
        self._global_add_block_reason = ""
        self._global_add_cooldown_left = 0.0
        long_dominant_block = self._dominant_side_add_block_reason("long")
        short_dominant_block = self._dominant_side_add_block_reason("short")
        dominant_side = self._dominant_side()
        if self._paired_add_triggered(dominant_side, price):
            paired = self._do_paired_add(dominant_side, price)
            if paired is not None:
                return paired
        # Long trigger: price <= grid_center * (1 - step * (long_layer + 1))
        if self._active_grid_side("long"):
            self._long_add_trigger_price = 0.0
            self._set_add_block("long", "passive_grid_active")
        elif self._grid_side_enabled("long") and self._long_layer < self._effective_side_max_adds("long"):
            long_step = self._effective_side_step("long")
            next_long = self._grid_center_price * (1.0 - long_step * (self._long_layer + 1))
            self._long_add_trigger_price = next_long
            if long_dominant_block:
                self._set_add_block("long", long_dominant_block)
                next_long = 0.0
                self._long_add_trigger_price = 0.0
            if not long_dominant_block and price <= next_long:
                long_cd = max(0.0, self._add_cooldown_sec - (time.time() - self._last_add_long))
                if long_cd > 0:
                    self._set_add_block("long", "side_cooldown", long_cd)
                    return None  # cooldown
                return self._do_add("long", price)
            if not long_dominant_block:
                self._set_add_block("long", "await_trigger")
        else:
            self._long_add_trigger_price = 0.0
            if self._long_layer >= self._effective_side_max_adds("long"):
                reason = "long_max_layers"
            elif self._recent_side_loss_active("long"):
                reason = "recent_loss_guard"
            else:
                reason = self._grid_side_block_reason("long") or "side_disabled"
            self._set_add_block("long", reason)
        # Short trigger: price >= grid_center * (1 + step * (short_layer + 1))
        if self._active_grid_side("short"):
            self._short_add_trigger_price = 0.0
            self._set_add_block("short", "passive_grid_active")
        elif self._grid_side_enabled("short") and self._short_layer < self._effective_side_max_adds("short"):
            short_step = self._effective_side_step("short")
            next_short = self._grid_center_price * (1.0 + short_step * (self._short_layer + 1))
            self._short_add_trigger_price = next_short
            if short_dominant_block:
                self._set_add_block("short", short_dominant_block)
                next_short = 0.0
                self._short_add_trigger_price = 0.0
            if not short_dominant_block and price >= next_short:
                short_cd = max(0.0, self._add_cooldown_sec - (time.time() - self._last_add_short))
                if short_cd > 0:
                    self._set_add_block("short", "side_cooldown", short_cd)
                    return None  # cooldown
                return self._do_add("short", price)
            if not short_dominant_block:
                self._set_add_block("short", "await_trigger")
        else:
            self._short_add_trigger_price = 0.0
            if self._short_layer >= self._effective_side_max_adds("short"):
                reason = "short_max_layers"
            elif self._recent_side_loss_active("short"):
                reason = "recent_loss_guard"
            else:
                reason = self._grid_side_block_reason("short") or "side_disabled"
            self._set_add_block("short", reason)
        return None

    def _apply_exchange_positions(self, positions) -> None:
        for side in ("long", "short"):
            current = self._long if side == "long" else self._short
            latest = positions[side]
            current.qty = latest.qty
            current.entry = latest.entry
            current.cost = abs(
                latest.qty * latest.entry * max(safe_float(getattr(self, "_ct_val", 1.0)), 1e-12)
                / max(1, int(safe_float(getattr(self, "_cur_lev", 1))))
            )

    def _estimate_adds(self, qty: float, entry: float) -> int:
        """根据持仓占最大仓位的比例估算加仓层数，避免小合约张数被误判满层"""
        if qty <= 0:
            return 0
        max_qty = max(float(getattr(self, "_max_long_pos", 0) or 0), float(getattr(self, "_max_short_pos", 0) or 0))
        if max_qty <= 0:
            return 0
        ratio = min(1.0, qty / max_qty)
        inferred = max(0, round(ratio * self._max_adds))
        return min(max(0, self._max_adds - 1), inferred)

    def _refresh_position_after_reduce(self, side: str, attempts: int = 4) -> dict:
        """Confirm a reduce-only fill before changing local inventory state."""
        if side not in {"long", "short"}:
            return {"confirmed": False, "reason": "invalid_side"}
        if not self._client:
            return {"confirmed": True, "flat": False, "qty": 0.0}
        empty_confirmations = 0
        last_error = ""
        for attempt in range(1, max(2, attempts) + 1):
            try:
                rows = self._fetch_position_rows()
                row = next(
                    (
                        item for item in rows
                        if str(item.get("posSide") or "").lower() == side
                        and abs(safe_float(item.get("pos", 0))) >= self._position_qty_tolerance()
                    ),
                    None,
                )
                if row is not None:
                    qty = abs(safe_float(row.get("pos", 0)))
                    entry = safe_float(row.get("avgPx", 0))
                    actual_lev = max(1, int(round(safe_float(row.get("lever")))) or self._cur_lev)
                    imr = safe_float(row.get("imr"))
                    cost = imr if imr > 0 else abs(qty * entry * max(self._ct_val, 1e-12) / actual_lev)
                    current = self._long if side == "long" else self._short
                    updated = Pos(side, entry or current.entry, qty, cost, current.adds)
                    updated.adds = self._estimate_adds(qty, updated.entry)
                    if side == "long":
                        self._long = updated
                        self._long_layer = updated.adds
                    else:
                        self._short = updated
                        self._short_layer = updated.adds
                    self._update_position_age(side, qty)
                    self._last_position_change_ts = time.time()
                    return {"confirmed": True, "flat": False, "qty": qty}
                empty_confirmations += 1
                if empty_confirmations >= 2:
                    return {"confirmed": True, "flat": True, "qty": 0.0}
            except Exception as exc:
                last_error = str(exc)
            if attempt < max(2, attempts):
                getattr(self, "_sleep", time.sleep)(min(0.5, 0.15 * attempt))
        return {"confirmed": False, "reason": "position_state_unavailable", "error": last_error}

    def _calibrate_layers(self):
        """每30秒校准_xxx_layer计数器, 与交易所真实持仓同步"""
        if not self._client: return
        now = time.time()
        if self._last_calibrate > 0 and now - self._last_calibrate < self._calibrate_interval: return
        self._last_calibrate = now
        try:
            rows = self._fetch_position_rows()
            for p in rows:
                self._observe_exchange_position_risk(p)
                qty = safe_float(p.get("pos", 0))
                if abs(qty) < self._position_qty_tolerance(): continue
                side = p.get("posSide")
                if side == "long":
                    if abs(qty - self._long.qty) > self._position_qty_tolerance():
                        self._long.qty = qty
                        self._long.entry = float(p.get("avgPx", self._long.entry))
                        self._long_layer = self._estimate_adds(qty, self._long.entry)
                        self.log.info(f"[校准] 多头: qty={qty:.4f} layer={self._long_layer}")
                elif side == "short":
                    if abs(qty - self._short.qty) > self._position_qty_tolerance():
                        self._short.qty = qty
                        self._short.entry = float(p.get("avgPx", self._short.entry))
                        self._short_layer = self._estimate_adds(qty, self._short.entry)
                        self.log.info(f"[校准] 空头: qty={qty:.4f} layer={self._short_layer}")
        except Exception as e:
            self.log.warning(f"[校准] 异常: {e}")

    def _check_circuit_breaker(self, price: float) -> Optional[str]:
        """Observe fast markets without stopping price-driven trading."""
        now = time.time()
        self._price_history.append((now, price))
        cutoff = now - 900
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.pop(0)
        if len(self._price_history) < 10:
            return None
        five_ago = now - 300
        fp = None
        for ts, p in self._price_history:
            if ts >= five_ago:
                fp = p
                break
        five_chg = abs(price - fp) / fp if fp and fp > 0 else 0.0
        fifteen_ago = now - 900
        fp2 = None
        for ts, p in self._price_history:
            if ts >= fifteen_ago:
                fp2 = p
                break
        fifteen_chg = abs(price - fp2) / fp2 if fp2 and fp2 > 0 else 0.0
        if (five_chg > 0.03 or fifteen_chg > 0.05) and now >= self._cb_pause_until:
            self._cb_pause_until = now + 60
            self.log.warning(
                f"[fast_market] 5min={five_chg:.1%} 15min={fifteen_chg:.1%}; trading continues"
            )
        return None

    def _wick_check(self, side: str, price: float) -> bool:
        if not getattr(self, "_wick_filter_enabled", True):
            return True
        if side not in {"long", "short"} or not self._client:
            return True
        try:
            candles = self._client.get_candles(self._symbol, bar="5m", limit=3)
        except Exception as e:
            self.log.warning(f"[wick] read failed: {e}")
            return True
        series = [c for c in candles if isinstance(c, dict)]
        if not series:
            return True
        series.sort(key=lambda c: int(c.get("ts", 0) or 0), reverse=True)
        candle = series[1] if len(series) > 1 else series[0]
        op = safe_float(candle.get("open", 0))
        hi = safe_float(candle.get("high", 0))
        lo = safe_float(candle.get("low", 0))
        cl = safe_float(candle.get("close", 0))
        if hi <= 0 or lo <= 0 or op <= 0 or cl <= 0 or hi <= lo:
            return True
        body = abs(cl - op)
        span = max(hi - lo, price * 0.0005, 1e-8)
        upper = max(0.0, hi - max(op, cl))
        lower = max(0.0, min(op, cl) - lo)
        body_ratio = body / span
        wick_ratio = self._moca_wick_ratio
        if side == "long":
            rejection = lower >= max(body * wick_ratio, span * 0.34)
            bearish_impulse = cl < op and body_ratio >= 0.58 and lower < body * 0.45
            if bearish_impulse and not rejection and price <= cl:
                self._wick_px = price
                self._wick_ts = time.time()
                self._wick_side = side
                self.log.info(f"[wick] skip long add body={body_ratio:.2f} lower={lower/max(body,1e-8):.2f}")
                return False
            return True
        rejection = upper >= max(body * wick_ratio, span * 0.34)
        bullish_impulse = cl > op and body_ratio >= 0.58 and upper < body * 0.45
        if bullish_impulse and not rejection and price >= cl:
            self._wick_px = price
            self._wick_ts = time.time()
            self._wick_side = side
            self.log.info(f"[wick] skip short add body={body_ratio:.2f} upper={upper/max(body,1e-8):.2f}")
            return False
        return True

    def _check_funding_arb(self):
        # Funding rate adjustment: only if reserve cash is still enough
        if not self._client or self._last_price <= 0:
            return
        if not self._can_open_new_position():
            return
        now = time.time()
        if getattr(self, "_funding_adjusted", False) and now - getattr(self, "_funding_adjust_ts", 0) < 3600:
            return
        try:
            rate = self._client.get_funding_rate(self._symbol)
            settl_ts = self._client.get_next_funding_time(self._symbol)
            if settl_ts <= 0:
                return
            if now < settl_ts - 300:
                return
            if abs(rate) < 0.0001:
                return
            total = self._long.qty + self._short.qty
            if total < self._min_qty * 10:
                return
            adjust_qty = contract_qty_for_margin(
                self._base_pos_usdt * 0.1,
                self._last_price,
                self._cur_lev,
                self._ct_val,
                self._qty_step,
                self._min_qty,
            )
            adjust_usdt = self._base_pos_usdt * 0.1
            if rate > 0:
                try:
                    if not self._can_spend_usdt(adjust_usdt):
                        self.log.info(f"[?????] skip short add, reserve block free={self._available_after_reserve():.2f} reserve={self._reserved_usdt:.2f}")
                        return
                    result = self._place_open_order("sell", adjust_qty, "short")
                    if not self._order_succeeded(result):
                        raise RuntimeError(f"place_order failed: {result}")
                    self._short.qty += adjust_qty
                    self._funding_adjust_side = "short"
                    self.log.info(f"[?????] ???={rate:.4%}, ??{adjust_qty}")
                except Exception as e:
                    self.log.warning(f"[?????] ????: {e}")
            else:
                try:
                    if not self._can_spend_usdt(adjust_usdt):
                        self.log.info(f"[?????] skip long add, reserve block free={self._available_after_reserve():.2f} reserve={self._reserved_usdt:.2f}")
                        return
                    result = self._place_open_order("buy", adjust_qty, "long")
                    if not self._order_succeeded(result):
                        raise RuntimeError(f"place_order failed: {result}")
                    self._long.qty += adjust_qty
                    self._funding_adjust_side = "long"
                    self.log.info(f"[?????] ???={rate:.4%}, ??{adjust_qty}")
                except Exception as e:
                    self.log.warning(f"[?????] ????: {e}")
            self._funding_adjusted = True
            self._funding_adjust_ts = now
            self._funding_adjust_qty = adjust_qty
        except Exception as e:
            self.log.warning(f"[?????] ??: {e}")

    def _revert_funding_arb(self):
        if not getattr(self, "_funding_adjusted", False):
            return
        now = time.time()
        if now - getattr(self, "_funding_adjust_ts", 0) < 600:
            return
        qty = getattr(self, "_funding_adjust_qty", 0)
        if qty <= 0:
            self._funding_adjusted = False
            return
        side = getattr(self, "_funding_adjust_side", "")
        try:
            if side == "long":
                self._submit_order(self._symbol, "sell", str(qty), pos_side="long", reduce_only=True)
                self._long.qty = max(0, self._long.qty - qty)
            elif side == "short":
                self._submit_order(self._symbol, "buy", str(qty), pos_side="short", reduce_only=True)
                self._short.qty = max(0, self._short.qty - qty)
            self.log.info(f"[资金费] 恢复中性, 回补{side} {qty}")
        except Exception as e:
            self.log.warning(f"[资金费] 恢复失败: {e}")
        self._funding_adjusted = False
        self._funding_adjust_qty = 0
        self._funding_adjust_side = ""

    def _init_funding_vars(self):
        if not hasattr(self, "_funding_adjusted"):
            self._funding_adjusted = False
        if not hasattr(self, "_funding_adjust_qty"):
            self._funding_adjust_qty = 0
        if not hasattr(self, "_funding_adjust_ts"):
            self._funding_adjust_ts = 0

    def _check_drift(self):
        import math
        if self._in_startup_recovery(): return None
        if self._in_post_recovery_refill_delay():
            return None
        if self._grid_center_price <= 0 or self._last_price <= 0: return None
        drift = abs(self._last_price - self._grid_center_price) / self._grid_center_price
        threshold = self._effective_drift_threshold()
        max_gl = max(1, int(self._target_grid_layers()))
        step_long = self._effective_side_step("long")
        step_short = self._effective_side_step("short")
        # A live level only needs repositioning when it can no longer serve the
        # grid: it sits beyond the outermost layer, or on the wrong side of the
        # current price (a buy above price would already have filled, likewise a
        # sell below).  Comparing every level against its nearest target treated
        # any small move as drift and rebuilt the whole ladder, which reset the
        # orders to the new price before they could ever fill.
        max_layer_index = max_gl + 2.0
        min_layer_index = 0.2
        levels_stale = False
        for side, levels in (("long", self._long_grid_levels), ("short", self._short_grid_levels)):
            step = step_long if side == "long" else step_short
            if step <= 0 or step >= 1.0:
                continue
            log_step = math.log(1.0 - step) if side == "long" else math.log(1.0 + step)
            if abs(log_step) < 1e-12:
                continue
            for level in levels or []:
                lv = safe_float(level)
                if lv <= 0:
                    continue
                ratio = lv / self._last_price
                if ratio <= 0:
                    continue
                if side == "long" and ratio >= 1.0:
                    levels_stale = True
                    break
                if side == "short" and ratio <= 1.0:
                    levels_stale = True
                    break
                layer_index = math.log(ratio) / log_step
                if layer_index > max_layer_index or layer_index < min_layer_index:
                    levels_stale = True
                    break
            if levels_stale:
                break
        if drift < threshold and not levels_stale: return None
        if levels_stale:
            self.log.info(f"[网格漂移] 挂单层级偏离现价目标, 重建")
        else:
            self.log.info(f"[网格漂移] {drift:.1%} > {threshold:.1%}, 重建")
        return self._rebuild_grid()

    def _check_trend_orders(self):
        if self._trading_halted: return
        if not hasattr(self, '_moca_state') or not self._client: return
        last = getattr(self, "_last_trend_check", "")
        if self._moca_state == last: return
        self._last_trend_check = self._moca_state
        counter_side = self._trend_counter_side()
        dominant = self._dominant_side()
        imbalance = self._net_imbalance_ratio()
        margin_load = 0.0
        if self._cached_equity > 0:
            margin_load = self._account_margin_load_ratio()
        # Trend execution stays disabled until exchange-hosted SL/TP algo
        # orders are acknowledged.  This prevents a future trend executor
        # from running a position with only local protection.
        self.log.info(
            f"[trend] state={self._moca_state} bias={counter_side or 'none'} "
            f"dominant={dominant or 'balanced'} imbalance={imbalance:.2%} margin={margin_load:.2%} "
            "monitor_only exchange_algo_protection_required"
        )

    def update_moca(self, params: dict):
        lev = params.get("leverage", 1.0); state = params.get("state", "")
        tp = params.get("tp_ratio", 1.0)
        add_layers = params.get("add_layers", 1.0)
        vol_step = params.get("vol_step", 1.0)
        atr_ratio = max(0.0, safe_float(params.get("atr_ratio", 0.0)))
        confidence = safe_float(params.get("confidence", 0.0))
        previous_observed = self._moca_observed_state
        self._moca_observed_state = state
        self._moca_observed_confidence = confidence
        self._moca_atr_ratio = atr_ratio
        if not self._moca_apply_live:
            self._moca_state = ""
            if state != previous_observed:
                self.log.info(f"[MOCA] observed={state or 'unknown'} confidence={confidence:.2f} monitor_only")
                _record_trend_event("moca", self._symbol, state=state, confidence=confidence, apply_live=0)
            return
        self._moca_state = state
        new_lev = max(1, min(int(self._base_lev * lev), self._base_lev))
        if new_lev != self._cur_lev:
            if self._client:
                try:
                    self._set_leverage(self._symbol, new_lev, self._margin_mode)
                except Exception as exc:
                    self.log.warning(f"[MOCA] leverage update failed; keeping {self._cur_lev}x: {exc}")
                    self.pause_opening("leverage_unavailable", 300.0, cancel_existing=True)
                else:
                    self._cur_lev = new_lev
            else:
                self._cur_lev = new_lev
        if state in ("trending_up", "trending_down"):
            self._max_adds = max(1, min(10, int(round(self._base_max_adds * max(0.7, add_layers * 0.8)))))
            self._first_tp = self._effective_tp_ratio(self._base_first_tp * max(0.95, tp), "taker")
            self._final_tp = max(self._first_tp + 0.003, self._base_final_tp * max(0.95, tp))
            self._add_step = max(0.001, self._base_add_step * max(0.9, vol_step))
            self._add_mult = min(self._base_add_mult, 1.05)
        elif state == "spike":
            self._max_adds = max(1, min(8, int(round(self._base_max_adds * max(0.5, add_layers * 0.6)))))
            self._first_tp = self._effective_tp_ratio(self._base_first_tp * max(1.05, tp), "taker")
            self._final_tp = max(self._first_tp + 0.004, self._base_final_tp * max(1.05, tp))
            self._add_step = max(0.001, self._base_add_step * max(1.15, vol_step * 1.15))
            self._add_mult = min(self._base_add_mult, 1.0)
        else:
            self._max_adds = max(1, min(12, int(round(self._base_max_adds * add_layers))))
            self._first_tp = self._effective_tp_ratio(self._base_first_tp * tp, "taker")
            self._final_tp = max(self._first_tp + 0.002, self._base_final_tp * tp)
            self._add_step = max(0.001, self._base_add_step * vol_step)
            self._add_mult = self._base_add_mult
        cur_state = (
            self._cur_lev,
            self._max_adds,
            round(self._first_tp, 3),
            round(self._final_tp, 3),
            round(self._add_step, 4),
            round(self._add_mult, 2),
        )
        self._moca_grid_step_multiplier = max(0.0, safe_float(vol_step, 1.0))
        if not hasattr(self, '_last_moca_state') or self._last_moca_state != cur_state:
            self._last_moca_state = cur_state
            self.log.info(
                f"[MOCA] lev={self._cur_lev}x adds={self._max_adds} "
                f"tp={self._first_tp:.3f}/{self._final_tp:.3f} "
                f"step={self._add_step:.4f} mult={self._add_mult:.2f}"
            )

    def _exchange_position_snapshot(self, side: str):
        """Return the authoritative exchange quantity/entry for one hedge side."""
        side = str(side or "").lower()
        if side not in {"long", "short"} or not self._client:
            return None
        try:
            rows = self._fetch_position_rows()
            for row in rows:
                if str(row.get("posSide") or "").lower() == side:
                    return (
                        abs(safe_float(row.get("pos", 0))),
                        safe_float(row.get("avgPx", 0)),
                    )
            return (0.0, 0.0)
        except Exception as exc:
            self.log.warning(f"[TP] position check failed side={side}: {exc}")
            return None

    def _position_qty_tolerance(self) -> float:
        from strategies.hedge_grid import position_qty_tolerance
        return position_qty_tolerance(safe_float(getattr(self, "_qty_step", 0.0)))

    def _exchange_side_flat_confirmed(self, side: str) -> bool:
        """确认某侧在交易所确实已平仓，仅在 API 健康时返回 True。

        网络/API 降级窗口内持仓查询可能返回空（code=0 data=[]），
        若把"查询无数据"当作"仓位已平"，会清空该侧 TP 保护并重置内部持仓，
        导致裸奔与死循环。API 降级期间一律视为"未知"，不做状态变更。"""
        client = self._client
        if client is None:
            return False
        degraded = getattr(client, "api_degraded", None)
        if callable(degraded):
            try:
                if degraded(60.0, 3):
                    return False
            except Exception:
                return False
        return True

    def on_order_result(self, result: dict):
        sc = result.get("sCode", "")
        if result.get("type") == "layer_tp" and sc == "0": self._active_tp_orders = max(0, self._active_tp_orders - 1)
        if sc != "0":
            self._consecutive_errors += 1
            if self._consecutive_errors >= 3:
                self._breaker_until = time.time() + self._breaker_cooldown
                self.log.warning(f"[熔断] {self._breaker_cooldown}s (连续{self._consecutive_errors}次错误)")
        else: self._consecutive_errors = max(0, self._consecutive_errors - 1)

    def reload_config(self, fc: dict):
        """热加载: 从引擎接收更新后的配置参数, 运行时生效"""
        changed = []
        leverage_candidate = None
        if "reduce_only_mode" in fc:
            reduce_only = bool(fc.get("reduce_only_mode"))
            if reduce_only != getattr(self, "_reduce_only_mode", False):
                self.set_reduce_only(reduce_only)
                changed.append(f"reduce_only_mode={reduce_only}")
        for key, attr in (
            ("active_inventory_repair_enabled", "_active_inventory_repair_enabled"),
            ("paired_add_enabled", "_paired_add_enabled"),
            ("paired_add_use_hard_total_cap", "_paired_add_use_hard_total_cap"),
            ("conditional_missing_side_repair_enabled", "_conditional_missing_side_repair_enabled"),
            ("missing_side_repair_allow_legacy_state", "_missing_side_repair_allow_legacy_state"),
        ):
            if key in fc:
                new_val = bool(fc.get(key))
                if new_val != getattr(self, attr, False):
                    setattr(self, attr, new_val)
                    changed.append(f"{key}={new_val}")
        fields = [
            ("leverage", "_base_lev", int, lambda v: max(1, min(v, 100))),
            ("base_position_usdt", "_base_pos_usdt", float, lambda v: max(1, v)),
            ("max_add_layers", "_max_adds", int, lambda v: max(1, min(v, 20))),
            ("add_step", "_add_step", float, lambda v: max(0.001, v)),
            ("add_multiplier", "_add_mult", float, lambda v: max(1.0, min(v, 10.0))),
            ("first_tp_ratio", "_first_tp", float, lambda v: max(0.001, v)),
            ("final_tp_ratio", "_final_tp", float, lambda v: max(0.001, v)),
            ("add_cooldown", "_cooldown", float, lambda v: max(1, v)),
            ("add_rate_limit_window_sec", "_add_rate_limit_window_sec", float, lambda v: max(30.0, v)),
            ("regular_add_rate_limit_margin_usdt", "_regular_add_rate_limit_margin_usdt", float, lambda v: max(0.0, v)),
            ("inventory_repair_rate_limit_margin_usdt", "_inventory_repair_rate_limit_margin_usdt", float, lambda v: max(0.0, v)),
            ("paired_add_rate_limit_margin_usdt", "_paired_add_rate_limit_margin_usdt", float, lambda v: max(0.0, v)),
            ("inventory_repair_imbalance_gate", "_inventory_repair_imbalance_gate", float, lambda v: max(0.0, min(v, 1.0))),
            ("inventory_repair_stage_ratio", "_inventory_repair_stage_ratio", float, lambda v: max(0.05, min(v, 0.5))),
            ("inventory_repair_exit_net_notional_usdt", "_inventory_repair_exit_net_notional_usdt", float, lambda v: max(0.0, v)),
            ("inventory_repair_exit_imbalance_ratio", "_inventory_repair_exit_imbalance_ratio", float, lambda v: max(0.0, min(v, 1.0))),
            ("inventory_repair_cooldown_sec", "_inventory_repair_cooldown_sec", float, lambda v: max(30.0, v)),
            ("inventory_repair_fast_cooldown_sec", "_inventory_repair_fast_cooldown_sec", float, lambda v: max(15.0, v)),
            ("inventory_repair_medium_cooldown_sec", "_inventory_repair_medium_cooldown_sec", float, lambda v: max(30.0, v)),
            ("paired_add_cooldown_sec", "_paired_add_cooldown_sec", float, lambda v: max(30.0, v)),
            ("paired_add_trigger_ratio", "_paired_add_trigger_ratio", float, lambda v: max(0.001, v)),
            ("missing_side_repair_imbalance_threshold", "_missing_side_repair_imbalance_threshold", float, lambda v: max(0.05, min(v, 1.0))),
            ("missing_side_repair_cooldown_sec", "_missing_side_repair_cooldown_sec", float, lambda v: max(30.0, v)),
            ("missing_side_repair_max_margin_ratio", "_missing_side_repair_max_margin_ratio", float, lambda v: max(0.05, min(v, 0.8))),
            ("working_total_notional_ratio", "_working_total_notional_ratio", float, lambda v: max(0.5, min(v, 1.0))),
            ("inventory_margin_stop_ratio", "_inventory_margin_stop_ratio", float, lambda v: max(0.0, min(v, 0.8))),
            ("inventory_margin_resume_ratio", "_inventory_margin_resume_ratio", float, lambda v: max(0.0, min(v, 0.8))),
            ("inventory_repair_missing_side_margin_ratio", "_inventory_repair_missing_side_margin_ratio", float, lambda v: max(0.0, min(v, 0.8))),
            ("inventory_repair_missing_side_resume_ratio", "_inventory_repair_missing_side_resume_ratio", float, lambda v: max(0.0, min(v, 0.8))),
            ("inventory_margin_hedge_credit_ratio", "_inventory_margin_hedge_credit_ratio", float, lambda v: max(0.0, min(v, 0.25))),
            ("account_margin_buffer_ratio", "_margin_buffer_ratio", float, lambda v: max(0.0, min(v, 0.5))),
            ("mgn_ratio_open_limit", "_mgn_ratio_open_limit", float, lambda v: max(0.0, v)),
            ("mgn_ratio_pause_limit", "_mgn_ratio_pause_limit", float, lambda v: max(0.0, v)),
            ("mgn_ratio_emergency_limit", "_mgn_ratio_emergency_limit", float, lambda v: max(0.0, v)),
            ("account_risk_max_age_sec", "_account_risk_max_age_sec", float, lambda v: max(5.0, v)),
            ("hard_stop_enabled", "_hard_stop_enabled", bool, lambda v: bool(v)),
            ("max_loss_usdt", "_max_loss_usdt", int, lambda v: max(1, v)),
            ("max_drawdown", "_max_drawdown", float, lambda v: max(0.01, min(v, 0.5))),
        ]
        for key, attr, typ, validator in fields:
            if key in fc:
                new_val = validator(typ(safe_float(fc[key])))
                old_val = getattr(self, attr, None)
                if old_val is not None and abs(new_val - old_val) > 0.001 * (abs(old_val) if old_val != 0 else 1):
                    if key == "leverage":
                        leverage_candidate = new_val
                    else:
                        setattr(self, attr, new_val)
                        changed.append(f"{key}={new_val}")
        if leverage_candidate is not None:
            if self._client:
                try:
                    self._set_leverage(self._symbol, leverage_candidate, self._margin_mode)
                except Exception as exc:
                    self.log.warning(f"[hot reload] leverage update failed; keeping {self._base_lev}x: {exc}")
                else:
                    self._base_lev = leverage_candidate
                    self._cur_lev = leverage_candidate
                    changed.append(f"leverage={leverage_candidate}")
            else:
                self._base_lev = leverage_candidate
                self._cur_lev = leverage_candidate
                changed.append(f"leverage={leverage_candidate}")
        if changed:
            self.log.info(f"[hot reload] strategy parameters updated: {', '.join(changed)}")

    def get_status(self) -> Dict:
        """Return the stable status schema assembled by the read-only module."""
        from strategies.hedge_status import build_status
        return build_status(self)
