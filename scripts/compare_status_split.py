# -*- coding: utf-8 -*-
"""
拆分前后 get_status() 输出对比验证
- 旧实现：从备份 futures_hedge.py.bak_status_split_20260914_113828 提取原 get_status 方法体，
          编译为独立函数 old_build_status(self)（globals 与旧模块一致：time/safe_float/Dict）
- 新实现：strategies.hedge_status.build_status(self)
- 同一策略实例（__new__ 构造 + 状态填充）分别调用两者，逐字段深度对比 JSON
"""
import ast
import sys
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, "/opt/okx_quant")
from strategies.futures_hedge import FuturesHedgeStrategy, HedgeInventory, Pos  # noqa: E402
from strategies.hedge_status import build_status as new_build_status  # noqa: E402
from utils.helpers import safe_float  # noqa: E402


def load_old_build_status(backup_path):
    src = open(backup_path, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_status":
            fn = node
            break
    if fn is None:
        raise RuntimeError("get_status not found in backup")
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    code = ast.unparse(body)
    g = {"time": time, "safe_float": safe_float, "Dict": dict}
    exec("def old_build_status(self):\n    " + code.replace("\n", "\n    "), g)
    return g["old_build_status"], code.splitlines()


def build_strategy():
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    # --- runtime/identity ---
    s._symbol = "DRAM-USDT-SWAP"
    s._profile_slug = "dram"
    s._profile_name = "DRAM"
    s._is_primary_profile = True
    s._running = True
    s._init_done = True
    s._reduce_only_mode = False
    s._trading_halted = True
    s._halt_reason = "hard_stop"
    s._halt_updated_at = 1757800000.0
    s._resume_required = True
    s._halt_operator = "manual"
    s._cur_lev = 5
    s._base_lev = 5
    s._exchange_leverage_by_side = {"long": 5, "short": 5}
    s._breaker_until = 0.0
    s._active_tp_orders = 6
    s._max_tp_orders = 10
    # --- positions ---
    s._inventory = HedgeInventory()
    s._inventory.long = Pos("long", 120.0, 0.1245, 0.0, 0)
    s._inventory.short = Pos("short", 95.0, 0.1251, 0.0, 0)
    s._long = s._inventory.long
    s._short = s._inventory.short
    s._max_long_pos = 6
    s._max_short_pos = 6
    s._last_price = 0.1258
    s._min_qty = 10.0
    s._qty_step = 10.0
    s._tick_size = 0.0001
    s._ct_val = 1.0
    # --- pnl / equity ---
    s._total_pnl = -3.24
    s._peak_pnl = 1.05
    s._total_fees = 1.87
    s._cached_avail = 612.5
    s._cached_frozen = 300.0
    s._cached_equity = 987.0
    s._risk_equity_baseline = 1000.0
    s._day_equity_baseline = 1000.0
    s._fee_pnl_file = "/tmp/compare/fee_pnl.json"
    s._halt_state_file = "/tmp/compare/halt_state.json"
    s._pnl_csv_path = "/tmp/compare/pnl_records.csv"
    s._daily_stop_until = 0.0
    s._daily_stop_reason = ""
    s._daily_stop_latched = False
    # --- segments / grid ---
    s._segments = {"long": [{"qty": 50}], "short": [{"qty": 40}]}
    s._long_grid_levels = [0.1230, 0.1220, 0.1210]
    s._short_grid_levels = [0.1270, 0.1280, 0.1290]
    s._target_grid_layers = lambda: 5
    s._grid_layer_limit_reason = lambda: ""
    s._grid_side_enabled = lambda side: True
    s._grid_side_block_reason = lambda side: ""
    s._grid_refill_block_reason = lambda side: ""
    s._grid_bias_ratio = 0.10
    # --- notional caps ---
    s._max_side_notional_usdt = 150.0
    s._max_long_side_notional_usdt = 150.0
    s._max_short_side_notional_usdt = 150.0
    s._max_total_notional_usdt = 300.0
    s._working_total_notional_ratio = 1.0
    # --- recovery / startup ---
    s._startup_recovery_active = False
    s._startup_recovery_seconds = 0
    s._startup_sync_ts = time.time()
    s._in_recovery_mode = lambda: False
    s._session_recovery_remaining_sec = lambda: 0.0
    s._in_post_recovery_refill_delay = lambda: False
    s._inventory_repair_cooldown = lambda net_notional: 120.0
    s._inventory_repair_cooldown_sec = 120.0
    s._last_inventory_repair_ts = 0.0
    # --- account risk ---
    s._account_risk_snapshot = {
        "usdt": {"mgnRatio": "15.2", "eq": "987", "availBal": "612.5"},
        "observed_at_ms": int(time.time() * 1000),
    }
    s._account_risk_max_age_sec = 30.0
    s._account_mgn_ratio = lambda: 15.2
    s._account_margin_data_block_reason = lambda: ""
    s._account_margin_load_ratio = lambda: 0.31
    s._strategy_notional_or_cost_ratio = lambda: 0.42
    s._mgn_ratio_open_limit = 5.0
    s._mgn_ratio_opening_block_reason = lambda: ""
    s._account_budget_snapshot = lambda: {"budget_usdt": 80.0, "used_usdt": 45.0}
    s._opening_guard_stats = lambda: {"opened": 3, "blocked": 1}
    s._account_margin_hard_limit = 0.55
    s._account_margin_soft_limit = 0.50
    s._account_margin_resume_limit = 0.49
    s._account_margin_scope = "dram"
    s._hard_margin_ratio = 0.55
    s._inventory_repair_max_margin_ratio = 0.45
    s._inventory_repair_missing_side_margin_ratio = 0.45
    s._inventory_repair_hard_override_enabled = False
    s._inventory_repair_hard_override_symbol = ""
    s._inventory_repair_hard_override_margin_ratio = 0.0
    s._inventory_repair_hard_override_mgn_ratio_min = 0.0
    s._inventory_repair_hard_override_max_step_notional_usdt = 0.0
    s._inventory_margin_thresholds = lambda: (0.50, 0.49, 0.35)
    s._inventory_opening_margin_blocked = lambda side: False
    s._inventory_margin_paused = False
    # --- inventory repair / over cap ---
    s._active_inventory_repair_enabled = True
    s._inventory_repair_mode = False
    s._inventory_repair_imbalance_gate = 0.70
    s._inventory_repair_exit_net_notional_usdt = 20.0
    s._inventory_repair_plan = lambda price: {
        "active": False,
        "repair_side": "",
        "proposed_qty": 0.0,
        "net_notional": 0.0,
        "imbalance_ratio": 0.10,
        "exit_ready": False,
        "entry_triggered": False,
    }
    s._net_imbalance_ratio = lambda: 0.116
    s._dominant_side = lambda: "long"
    s._effective_base_pos_usdt = lambda: 90.0
    s._over_cap_unwind_enabled = True
    s._over_cap_unwind_mode = False
    s._over_cap_unwind_last_reason = ""
    s._over_cap_unwind_last_result = ""
    s._over_cap_unwind_cooldown_sec = 60.0
    s._last_over_cap_unwind_ts = 0.0
    s._add_rate_limit_window_sec = 300.0
    s._regular_add_rate_limit_margin_usdt = 15.0
    s._inventory_repair_rate_limit_margin_usdt = 45.0
    s._paired_add_rate_limit_margin_usdt = 45.0
    # --- side config / paired / missing side ---
    s._side_config = {"long": {}, "short": {}}
    s._paired_add_enabled = False
    s._paired_add_cooldown_sec = 120.0
    s._last_paired_add_ts = 0.0
    s._paired_add_trigger_ratio = 0.0075
    s._paired_add_anchor_price = 0.0
    s._conditional_missing_side_repair_enabled = False
    s._missing_side_repair_allow_legacy_state = False
    s._missing_side_repair_imbalance_threshold = 0.70
    s._missing_side_repair_cooldown_sec = 180.0
    s._last_missing_side_repair_ts = 0.0
    s._missing_side_repair_last_reason = ""
    s._normal_close_sides = {}
    # --- tp coverage / order tracker ---
    s._exchange_tp_coverage_ready = True
    s._exchange_tp_coverage_at = time.time()
    s._risk_tp_covered_qty = lambda side: 120.0 if side == "long" else 95.0
    s._position_qty_tolerance = lambda: 5.0
    s._order_tracker = SimpleNamespace(summary=lambda: {
        "active_avg_age_sec": 12.3,
        "active_max_age_sec": 40.1,
        "grid_max_age_sec": 38.0,
        "tp_max_age_sec": 25.0,
        "total_active": 4,
    })
    # --- liquidation ---
    s._liquidation_status = {
        "liq_px_unavailable": False,
        "liquidation_distance_safe": True,
        "liquidation_warning": False,
        "liquidation_emergency": False,
        "zone": "safe",
        "reason": "",
        "min_distance_rate": 0.35,
        "mark_px": 0.1258,
    }
    s.opening_pause_status = lambda: {"active": False, "reason": "", "remaining_sec": 0.0}
    # --- position helpers ---
    s._position_age_sec = lambda side: 3600.0
    s._position_unrealized_pnl = lambda side: 1.2 if side == "long" else -0.8
    s._inventory_age_multiplier = lambda side: 1.0
    s._floating_loss_ratio = 1.0
    s._floating_loss_freeze_active = False
    # --- dynamic notional ---
    s._dynamic_notional_limits = lambda: {
        "side_cap": 150.0,
        "long_cap": 150.0,
        "short_cap": 150.0,
        "total_cap": 300.0,
        "scale": 1.0,
        "scaled_total_cap": 300.0,
        "margin_budget_usdt": 0.0,
        "margin_based_total_cap": 0.0,
        "margin_health": 1.0,
        "net_ratio": 0.08,
    }
    # --- moca ---
    s._moca_apply_live = False
    s._moca_observed_state = "calm"
    s._moca_observed_confidence = 0.8
    s._moca_atr_ratio = 0.0
    s._moca_grid_step_multiplier = 1.0
    s._add_step = 0.003
    # --- add gates ---
    s._global_add_block_reason = ""
    s._global_add_cooldown_left = 0.0
    s._long_add_trigger_price = 0.0
    s._short_add_trigger_price = 0.0
    s._long_add_block_reason = ""
    s._short_add_block_reason = ""
    s._long_add_cooldown_left = 0.0
    s._short_add_cooldown_left = 0.0
    # --- events ---
    s._last_event_type = "tp_fill"
    s._last_event_side = "long"
    s._last_event_reason = "first_tp"
    s._last_event_detail = "qty 10"
    s._last_event_price = 0.1265
    s._last_event_qty = 10.0
    s._last_event_ts = int(time.time())
    # --- session/daily pnl ---
    s._session_realized_pnl = lambda: 0.55
    s._daily_realized_pnl = lambda: -1.10
    return s


def build_strategy_edge():
    """边界场景：零价格、halted、开仓暂停、库存修复激活、无交易所TP覆盖、空订单追踪器。"""
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s._symbol = "FARTCOIN-USDT-SWAP"
    s._profile_slug = "fartcoin"
    s._profile_name = "FARTCOIN"
    s._is_primary_profile = False
    s._running = False
    s._init_done = False
    s._reduce_only_mode = True
    s._trading_halted = True
    s._halt_reason = "hard_stop"
    s._halt_updated_at = 0.0
    s._resume_required = False
    s._halt_operator = ""
    s._cur_lev = 3
    s._base_lev = 3
    s._exchange_leverage_by_side = {}
    s._breaker_until = time.time() + 3600
    s._active_tp_orders = 0
    s._max_tp_orders = 8
    s._inventory = HedgeInventory()
    s._inventory.long = Pos("long", 0.0, 0.0, 0.0, 0)
    s._inventory.short = Pos("short", 0.0, 0.0, 0.0, 0)
    s._long = s._inventory.long
    s._short = s._inventory.short
    s._max_long_pos = 5
    s._max_short_pos = 5
    s._last_price = 0.0
    s._min_qty = 100.0
    s._qty_step = 100.0
    s._tick_size = 0.0
    s._ct_val = 1.0
    s._total_pnl = 0.0
    s._peak_pnl = 0.0
    s._total_fees = 0.0
    s._cached_avail = 0.0
    s._cached_frozen = 0.0
    s._cached_equity = 0.0
    s._risk_equity_baseline = 0.0
    s._day_equity_baseline = 0.0
    s._fee_pnl_file = "/tmp/compare/fee_pnl2.json"
    s._halt_state_file = "/tmp/compare/halt_state2.json"
    s._pnl_csv_path = "/tmp/compare/pnl2.csv"
    s._daily_stop_until = time.time() + 3600
    s._daily_stop_reason = "daily_loss"
    s._daily_stop_latched = True
    s._segments = {"long": [], "short": []}
    s._long_grid_levels = []
    s._short_grid_levels = []
    s._target_grid_layers = lambda: 3
    s._grid_layer_limit_reason = lambda: "hard_stop"
    s._grid_side_enabled = lambda side: False
    s._grid_side_block_reason = lambda side: "halted"
    s._grid_refill_block_reason = lambda side: "halted"
    s._grid_bias_ratio = 0.0
    s._max_side_notional_usdt = 0.0
    s._max_long_side_notional_usdt = 0.0
    s._max_short_side_notional_usdt = 0.0
    s._max_total_notional_usdt = 0.0
    s._working_total_notional_ratio = 1.0
    s._startup_recovery_active = True
    s._startup_recovery_seconds = 300.0
    s._startup_sync_ts = time.time()
    s._in_recovery_mode = lambda: True
    s._session_recovery_remaining_sec = lambda: 120.0
    s._in_post_recovery_refill_delay = lambda: True
    s._inventory_repair_cooldown = lambda net_notional: 45.0
    s._inventory_repair_cooldown_sec = 120.0
    s._last_inventory_repair_ts = time.time() - 30
    s._account_risk_snapshot = {
        "usdt": {"mgnRatio": "3.2", "eq": "0.00025", "availBal": "0.00025"},
        "observed_at_ms": int(time.time() * 1000),
    }
    s._account_risk_max_age_sec = 30.0
    s._account_mgn_ratio = lambda: 0.0
    s._account_margin_data_block_reason = lambda: "stale"
    s._account_margin_load_ratio = lambda: 0.0
    s._strategy_notional_or_cost_ratio = lambda: 0.0
    s._mgn_ratio_open_limit = 5.0
    s._mgn_ratio_opening_block_reason = lambda: "mgn_ratio_unavailable"
    s._account_budget_snapshot = lambda: {}
    s._opening_guard_stats = lambda: {"opened": 0, "blocked": 0}
    s._account_margin_hard_limit = 0.55
    s._account_margin_soft_limit = 0.50
    s._account_margin_resume_limit = 0.49
    s._account_margin_scope = "fartcoin"
    s._hard_margin_ratio = 0.55
    s._inventory_repair_max_margin_ratio = 0.45
    s._inventory_repair_missing_side_margin_ratio = 0.45
    s._inventory_repair_hard_override_enabled = False
    s._inventory_repair_hard_override_symbol = ""
    s._inventory_repair_hard_override_margin_ratio = 0.0
    s._inventory_repair_hard_override_mgn_ratio_min = 0.0
    s._inventory_repair_hard_override_max_step_notional_usdt = 0.0
    s._inventory_margin_thresholds = lambda: (0.55, 0.54, 0.45)
    s._inventory_opening_margin_blocked = lambda side: True
    s._inventory_margin_paused = True
    s._active_inventory_repair_enabled = True
    s._inventory_repair_mode = True
    s._inventory_repair_imbalance_gate = 0.70
    s._inventory_repair_exit_net_notional_usdt = 10.0
    s._inventory_repair_plan = lambda price: {
        "active": True,
        "repair_side": "long",
        "proposed_qty": 50.0,
        "net_notional": -4.0,
        "imbalance_ratio": -0.8,
        "exit_ready": False,
        "entry_triggered": True,
    }
    s._net_imbalance_ratio = lambda: -0.8
    s._dominant_side = lambda: "short"
    s._effective_base_pos_usdt = lambda: 0.0
    s._over_cap_unwind_enabled = False
    s._over_cap_unwind_mode = False
    s._over_cap_unwind_last_reason = ""
    s._over_cap_unwind_last_result = ""
    s._over_cap_unwind_cooldown_sec = 60.0
    s._last_over_cap_unwind_ts = 0.0
    s._add_rate_limit_window_sec = 300.0
    s._regular_add_rate_limit_margin_usdt = 15.0
    s._inventory_repair_rate_limit_margin_usdt = 45.0
    s._paired_add_rate_limit_margin_usdt = 45.0
    s._side_config = {"long": {}, "short": {}}
    s._paired_add_enabled = False
    s._paired_add_cooldown_sec = 120.0
    s._last_paired_add_ts = 0.0
    s._paired_add_trigger_ratio = 0.0075
    s._paired_add_anchor_price = 0.0
    s._conditional_missing_side_repair_enabled = False
    s._missing_side_repair_allow_legacy_state = False
    s._missing_side_repair_imbalance_threshold = 0.70
    s._missing_side_repair_cooldown_sec = 180.0
    s._last_missing_side_repair_ts = 0.0
    s._missing_side_repair_last_reason = ""
    s._normal_close_sides = {"long": False, "short": True}
    s._exchange_tp_coverage_ready = False
    s._exchange_tp_coverage_at = 0.0
    s._risk_tp_covered_qty = lambda side: 0.0
    s._position_qty_tolerance = lambda: 0.0
    s._order_tracker = SimpleNamespace(summary=lambda: {})
    s._liquidation_status = {}
    s.opening_pause_status = lambda: {"active": True, "reason": "abnormal_market", "remaining_sec": 59.5}
    s._position_age_sec = lambda side: 0.0
    s._position_unrealized_pnl = lambda side: 0.0
    s._inventory_age_multiplier = lambda side: 1.0
    s._floating_loss_ratio = 0.6
    s._floating_loss_freeze_active = True
    s._dynamic_notional_limits = lambda: {
        "side_cap": 0.0,
        "long_cap": 0.0,
        "short_cap": 0.0,
        "total_cap": 0.0,
        "scale": 0.0,
        "scaled_total_cap": 0.0,
        "margin_budget_usdt": 0.0,
        "margin_based_total_cap": 0.0,
        "margin_health": 0.0,
        "net_ratio": 0.0,
    }
    s._moca_apply_live = False
    s._moca_observed_state = None
    s._moca_observed_confidence = 0.0
    s._moca_atr_ratio = 0.0
    s._moca_grid_step_multiplier = 1.0
    s._add_step = 0.0
    s._global_add_block_reason = "halted"
    s._global_add_cooldown_left = 300.0
    s._long_add_trigger_price = 0.0
    s._short_add_trigger_price = 0.0
    s._long_add_block_reason = "halted"
    s._short_add_block_reason = "halted"
    s._long_add_cooldown_left = 120.0
    s._short_add_cooldown_left = 120.0
    s._last_event_type = ""
    s._last_event_side = ""
    s._last_event_reason = ""
    s._last_event_detail = ""
    s._last_event_price = 0.0
    s._last_event_qty = 0.0
    s._last_event_ts = 0
    s._session_realized_pnl = lambda: 0.0
    s._daily_realized_pnl = lambda: 0.0
    return s


def deep_diff(a, b, path=""):
    """Return list of (path, old_val, new_val) differences."""
    diffs = []
    if type(a) is not type(b):
        return [(path, repr(a), repr(b), "TYPE")]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            p = f"{path}.{k}" if path else k
            if k not in a:
                diffs.append((p, "<missing>", repr(b[k]), "NEW_KEY"))
            elif k not in b:
                diffs.append((p, repr(a[k]), "<missing>", "OLD_KEY"))
            else:
                diffs.extend(deep_diff(a[k], b[k], p))
    elif isinstance(a, list):
        if len(a) != len(b):
            diffs.append((path, repr(a), repr(b), "LIST_LEN"))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                diffs.extend(deep_diff(x, y, f"{path}[{i}]"))
    elif isinstance(a, float):
        if a != b:
            diffs.append((path, repr(a), repr(b), "FLOAT"))
    else:
        if a != b:
            diffs.append((path, repr(a), repr(b), "SCALAR"))
    return diffs


def main():
    backup = "/opt/okx_quant/strategies/futures_hedge.py.bak_status_split_20260914_113828"
    old_build_status, old_body_lines = load_old_build_status(backup)
    scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"
    s = build_strategy() if scenario == "normal" else build_strategy_edge()

    # --- debug: catch round(None) ---
    import builtins
    _orig_round = builtins.round

    def _dbg_round(x, n=None):
        if x is None:
            import inspect, dis
            fr = inspect.currentframe().f_back
            print("ROUND(None) at", fr.f_code.co_filename, fr.f_lineno, "f_lasti=", fr.f_lasti)
            instrs = list(dis.get_instructions(fr.f_code))
            lo, hi = max(0, fr.f_lasti - 260), fr.f_lasti + 40
            for ins in instrs:
                if lo <= ins.offset <= hi:
                    mark = " >>>" if ins.offset - 2 <= fr.f_lasti <= ins.offset + 2 else ""
                    print(f"   {mark} {ins.offset:6d} {ins.opname:24s} {ins.argval}")
        return _orig_round(x, n) if n is not None else _orig_round(x)

    builtins.round = _dbg_round

    try:
        old_json = old_build_status(s)
    except Exception:
        print("OLD FAILED:")
        tb = traceback.extract_tb(sys.exc_info()[2])
        for frame in tb:
            print(f"  {frame.filename}:{frame.lineno} {frame.name}: {frame.line}")
        ln = sys.exc_info()[2].tb_next.tb_lineno if sys.exc_info()[2].tb_next else 0
        if ln and old_body_lines:
            print("  body line:", old_body_lines[ln - 2][:160])
        return 1
    try:
        new_json = new_build_status(s)
    except Exception:
        print("NEW FAILED:")
        traceback.print_exc()
        return 1

    old_keys = set(old_json)
    new_keys = set(new_json)
    print("old keys:", len(old_keys), " new keys:", len(new_keys))
    print("keys only in old:", sorted(old_keys - new_keys))
    print("keys only in new:", sorted(new_keys - old_keys))

    diffs = deep_diff(old_json, new_json)
    if not diffs:
        print("RESULT: PASS — JSON 字段与数值完全一致 (fields=%d)" % len(old_keys))
        return 0
    print("RESULT: FAIL — %d differences" % len(diffs))
    for path, ov, nv, kind in diffs[:100]:
        print(f"  [{kind}] {path}: old={ov} new={nv}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
