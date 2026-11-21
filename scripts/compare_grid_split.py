# -*- coding: utf-8 -*-
"""拆分第四阶段影子对比：网格与库存编排（旧实现 vs 新代理）。

- 旧实现：从备份 futures_hedge.py.bak_calc_split_20260914_131631 提取方法体，
  编译为 old_<name>(self, ...)，内部对已转换方法的 self 调用改写为旧实现
  （如 _maybe_active_inventory_repair 内调 _pending_repair_qty → old 版本）。
- 新实现：当前 futures_hedge.py 中的代理方法（指向 strategies.hedge_grid /
  strategies.hedge_inventory_repair）。
- 编排方法有状态写入，因此旧/新分别在两个独立构建的实例上运行（同一构建器、
  同一冻结时间），消除状态串扰造成的假差异。
"""
import ast
import math
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/opt/okx_quant")
from strategies.futures_hedge import FuturesHedgeStrategy, Pos  # noqa: E402
from utils.helpers import safe_float  # noqa: E402

METHODS = [
    "_pending_open_qty", "_inventory_repair_step_qty_cap",
    "_preserve_opening_grids_during_inventory_repair",
    "_inventory_repair_margin_blocked", "_pending_repair_qty",
    "_position_qty_tolerance",
    "_maybe_repair_normal_close_missing_side",
    "_maybe_active_inventory_repair",
    "_maybe_over_cap_unwind",
]

_OLD_NAMES = {m: "old_" + m for m in METHODS}
FIXED_NOW = 1757800000.0


class _RewriteSelfCalls(ast.NodeTransformer):
    def visit_Call(self, node):
        node = self.generic_visit(node)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in _OLD_NAMES
        ):
            return ast.Call(
                func=ast.Name(id=_OLD_NAMES[node.func.attr], ctx=ast.Load()),
                args=[ast.Name(id="self", ctx=ast.Load())] + list(node.args),
                keywords=list(node.keywords),
            )
        return node


def load_old_methods(backup):
    src = open(backup, encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    g = {"time": time, "safe_float": safe_float, "math": math}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in METHODS:
            old = ast.FunctionDef(
                name=_OLD_NAMES[node.name],
                args=node.args,
                body=list(node.body),
                decorator_list=[],
                returns=node.returns,
                type_comment=node.type_comment,
            )
            rewritten = _RewriteSelfCalls().visit(old)
            ast.fix_missing_locations(rewritten)
            code = ast.unparse(rewritten)
            exec(code, g)
    for name in METHODS:
        out[name] = g[_OLD_NAMES[name]]
    return out


class FakeInventory:
    def __init__(self, plan):
        self._plan = plan

    def repair_plan(self, *args, **kwargs):
        return dict(self._plan)


def _pos(qty):
    return Pos("long" if qty >= 0 else "short", abs(qty), 0.125, 0.0, 0)


def base_state():
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    s._client = object()
    s._trading_halted = False
    s._reduce_only_mode = False
    s._symbol = "CRV-USDT-SWAP"
    s._risk_opening_pause_active = lambda: False
    s._in_startup_recovery = lambda: False
    s._in_post_recovery_refill_delay = lambda: False
    s._in_recovery_mode = lambda: False
    s._account_margin_load_ratio = lambda: 0.25
    s._account_margin_data_block_reason = lambda: ""
    s._mgn_ratio_opening_block_reason = lambda: ""
    s._dynamic_notional_limits = lambda: {
        "side_cap": 150.0, "long_cap": 150.0, "short_cap": 150.0, "total_cap": 300.0,
    }
    s._margin_first_capacity_ready = lambda: False
    s._margin_first_capacity_enabled = False
    s._net_imbalance_ratio = lambda: 0.80
    s._effective_side_step = lambda side: 0.003
    s._side_config = {"long": {"add_step": 0.004, "max_add_layers": 4}, "short": {"add_step": 0.004, "max_add_layers": 4}}
    s._max_adds = 6
    s._fetch_live_orders = lambda: []
    s._pending_repair_qty_override = None  # unused hook
    s._qty_step = 0.1
    s._min_qty = 1.0
    s._ct_val = 1.0
    s._cur_lev = 5.0
    s._base_pos_usdt = 50.0
    s._recovery_pos_multiplier = 0.5
    s._cached_equity = 987.0
    s._tp_init_done = True
    s._risk_tp_covered_qty = lambda side: 120.0
    s._do_add = lambda side, price, **kw: {"type": "add", "quantity": "8.0"}
    s._submit_order = lambda *a, **k: {"sCode": "0", "ordId": "ord-123", "data": []}
    s._cancel_opening_orders = lambda: 0
    s._record_event = lambda *a, **k: None
    s._consume_coverage_safety_tp_repair = lambda side: None
    s._refresh_position_after_reduce = lambda side: {"confirmed": True, "qty": 100.0}
    s._init_existing_tp = lambda cancel_existing=False: None
    # 注意：_long/_short 是 property，底层读写 _inventory.long/.short；
    # 必须先设置 _inventory 再设置 _long/_short。
    s._inventory = FakeInventory({
        "active": True, "repair_side": "short", "proposed_qty": 20.0,
        "net_notional": 10.0, "stage": 1, "qty": 5.0,
    })
    s._long = _pos(120.0)
    s._short = _pos(95.0)
    s._long_layer = 0
    s._short_layer = 0
    # --- 库存修复 ---
    s._active_inventory_repair_enabled = True
    s._inventory_repair_ignore_layer_cap = False
    s._inventory_repair_hard_override_enabled = False
    s._inventory_repair_hard_override_max_step_notional_usdt = 0.0
    s._inventory_repair_hard_override_decision = lambda side, price, qty: {"allowed": True}
    s._account_margin_hard_limit = 0.55
    s._hard_margin_ratio = 0.0
    s._inventory_repair_missing_side_margin_ratio = 0.0
    s._max_net_notional_usdt = 60.0
    s._inventory_repair_imbalance_gate = 0.70
    s._inventory_repair_stage_ratio = 0.30
    s._inventory_repair_mode = False
    s._inventory_repair_exit_net_notional_usdt = 48.0
    s._inventory_repair_exit_imbalance_ratio = 0.45
    s._inventory_repair_fast_cooldown_sec = 30.0
    s._inventory_repair_medium_cooldown_sec = 60.0
    s._inventory_repair_cooldown_sec = 120.0
    s._last_inventory_repair_ts = FIXED_NOW - 1000.0
    s._coverage_safety_tp_repair_incident = {}
    s._hedged_grid_allow_dominant_refill = False
    # --- 缺边修复 ---
    s._conditional_missing_side_repair_enabled = False
    s._last_missing_side_repair_ts = FIXED_NOW - 1000.0
    s._missing_side_repair_cooldown_sec = 180.0
    s._normal_close_sides = {}
    s._missing_side_repair_allow_legacy_state = False
    s._missing_side_repair_imbalance_threshold = 0.70
    s._missing_side_repair_max_margin_ratio = 0.30
    s._missing_side_repair_last_reason = ""
    # --- 超额回撤 ---
    s._over_cap_unwind_enabled = False
    s._over_cap_unwind_mode = False
    s._over_cap_unwind_cooldown_sec = 60.0
    s._last_over_cap_unwind_ts = FIXED_NOW - 1000.0
    s._over_cap_unwind_target_ratio = 0.90
    s._over_cap_unwind_stage_ratio = 0.15
    s._over_cap_unwind_min_notional_usdt = 6.0
    s._over_cap_unwind_max_notional_usdt = 36.0
    s._over_cap_unwind_last_reason = ""
    s._over_cap_unwind_last_result = ""
    return s


def v_repair_disabled():
    s = base_state()
    s._active_inventory_repair_enabled = False
    return s


def v_repair_max_layers():
    s = base_state()
    s._short_layer = 4
    return s


def v_repair_pending():
    s = base_state()
    s._fetch_live_orders = lambda: [
        {"reduceOnly": "false", "posSide": "short", "side": "sell", "px": "0.125", "sz": "19"},
    ]
    return s


def v_repair_cooldown():
    s = base_state()
    s._last_inventory_repair_ts = FIXED_NOW - 10.0
    return s


def v_repair_margin_blocked():
    s = base_state()
    s._account_margin_load_ratio = lambda: 0.60
    s._inventory_repair_hard_override_decision = lambda side, price, qty: {"allowed": False}
    return s


def v_repair_capacity_exhausted():
    s = base_state()
    s._dynamic_notional_limits = lambda: {
        "side_cap": 0.0, "long_cap": 0.0, "short_cap": 0.0, "total_cap": 0.0,
    }
    s._max_net_notional_usdt = 0.0
    return s


def v_repair_do_add_skip():
    s = base_state()
    s._do_add = lambda side, price, **kw: {"type": "skip", "reason": "test_skip"}
    return s


def v_missing_side_ready():
    s = base_state()
    s._conditional_missing_side_repair_enabled = True
    s._normal_close_sides = {"long": True}
    s._long = _pos(0.0)
    s._short = _pos(120.0)
    return s


def v_missing_side_not_normal_close():
    s = v_missing_side_ready()
    s._normal_close_sides = {}
    return s


def v_missing_side_imbalance_low():
    s = v_missing_side_ready()
    s._net_imbalance_ratio = lambda: 0.40
    return s


def v_missing_side_margin_cap():
    s = v_missing_side_ready()
    s._account_margin_load_ratio = lambda: 0.35
    return s


def v_missing_side_cooldown():
    s = v_missing_side_ready()
    s._last_missing_side_repair_ts = FIXED_NOW - 10.0
    return s


def v_missing_side_pending_open():
    s = v_missing_side_ready()
    s._fetch_live_orders = lambda: [
        {"reduceOnly": "false", "posSide": "long", "ordType": "market", "sz": "3"},
    ]
    return s


def v_missing_side_tp_not_init():
    s = v_missing_side_ready()
    s._tp_init_done = False
    return s


def v_missing_side_tp_uncovered():
    s = v_missing_side_ready()
    s._risk_tp_covered_qty = lambda side: 50.0
    return s


def v_missing_side_do_add_skip():
    s = v_missing_side_ready()
    s._do_add = lambda side, price, **kw: {"type": "skip", "reason": "test_skip"}
    return s


def v_unwind_disabled():
    s = base_state()
    return s


def v_unwind_not_crv():
    s = base_state()
    s._over_cap_unwind_enabled = True
    s._symbol = "BTC-USDT-SWAP"
    return s


def v_unwind_soft_only():
    s = base_state()
    s._over_cap_unwind_enabled = True
    s._account_margin_load_ratio = lambda: 0.20
    return s


def v_unwind_success():
    s = base_state()
    s._over_cap_unwind_enabled = True
    s._account_margin_load_ratio = lambda: 0.60
    s._long = _pos(180.0)
    s._short = _pos(20.0)
    s._dynamic_notional_limits = lambda: {
        "side_cap": 150.0, "long_cap": 20.0, "short_cap": 20.0, "total_cap": 100.0,
    }
    s._refresh_position_after_reduce = lambda side: {"confirmed": True, "qty": 120.0}
    return s


def v_unwind_no_reduction():
    s = v_unwind_success()
    s._refresh_position_after_reduce = lambda side: {"confirmed": True, "qty": 180.0}
    return s


def v_unwind_unconfirmed():
    s = v_unwind_success()
    s._refresh_position_after_reduce = lambda side: {"confirmed": False, "qty": 180.0}
    return s


def v_unwind_rejected():
    s = v_unwind_success()
    s._submit_order = lambda *a, **k: {"sCode": "51000", "data": []}
    return s


def v_unwind_cooldown():
    s = v_unwind_success()
    s._last_over_cap_unwind_ts = FIXED_NOW - 10.0
    return s


def v_unwind_below_min():
    s = v_unwind_success()
    s._dynamic_notional_limits = lambda: {
        "side_cap": 150.0, "long_cap": 179.9, "short_cap": 179.9, "total_cap": 400.0,
    }
    s._over_cap_unwind_min_notional_usdt = 1000.0
    return s


def v_unwind_no_position():
    s = base_state()
    s._over_cap_unwind_enabled = True
    s._account_margin_load_ratio = lambda: 0.60
    s._long = _pos(0.0)
    s._short = _pos(0.0)
    return s


VARIANTS = {
    "normal": base_state,
    "repair_disabled": v_repair_disabled,
    "repair_max_layers": v_repair_max_layers,
    "repair_pending": v_repair_pending,
    "repair_cooldown": v_repair_cooldown,
    "repair_margin_blocked": v_repair_margin_blocked,
    "repair_capacity_exhausted": v_repair_capacity_exhausted,
    "repair_do_add_skip": v_repair_do_add_skip,
    "missing_side_ready": v_missing_side_ready,
    "missing_side_not_normal_close": v_missing_side_not_normal_close,
    "missing_side_imbalance_low": v_missing_side_imbalance_low,
    "missing_side_margin_cap": v_missing_side_margin_cap,
    "missing_side_cooldown": v_missing_side_cooldown,
    "missing_side_pending_open": v_missing_side_pending_open,
    "missing_side_tp_not_init": v_missing_side_tp_not_init,
    "missing_side_tp_uncovered": v_missing_side_tp_uncovered,
    "missing_side_do_add_skip": v_missing_side_do_add_skip,
    "unwind_disabled": v_unwind_disabled,
    "unwind_not_crv": v_unwind_not_crv,
    "unwind_soft_only": v_unwind_soft_only,
    "unwind_success": v_unwind_success,
    "unwind_no_reduction": v_unwind_no_reduction,
    "unwind_unconfirmed": v_unwind_unconfirmed,
    "unwind_rejected": v_unwind_rejected,
    "unwind_cooldown": v_unwind_cooldown,
    "unwind_below_min": v_unwind_below_min,
    "unwind_no_position": v_unwind_no_position,
}

ARG_MATRIX = {
    "_pending_open_qty": [("long",), ("short",), ("bad",)],
    "_inventory_repair_step_qty_cap": [
        ("long", 0.125, 30.0), ("long", 0.125, 5.0), ("short", 0.125, 30.0),
        ("long", 0.0, 30.0), ("long", 0.125, 0.0),
    ],
    "_preserve_opening_grids_during_inventory_repair": [()],
    "_inventory_repair_margin_blocked": [(), ("long", 0.125, 5.0)],
    "_pending_repair_qty": [("long", 0.125), ("short", 0.125), ("long", 0.0)],
    "_position_qty_tolerance": [()],
    "_maybe_repair_normal_close_missing_side": [(0.125,), (0.0,)],
    "_maybe_active_inventory_repair": [(0.125,), (0.0,)],
    "_maybe_over_cap_unwind": [(0.125,), (0.0,)],
}


def run_variant(name, builder, old_methods):
    mismatches = 0
    checked = 0
    for m in METHODS:
        old_fn = old_methods.get(m)
        for args in ARG_MATRIX.get(m, [()]):
            checked += 1
            s_old = builder()
            s_new = builder()
            try:
                old_val = old_fn(s_old, *args)
                old_err = None
            except Exception as e:
                old_val, old_err = None, f"{type(e).__name__}: {e}"
            try:
                new_val = getattr(s_new, m)(*args)
                new_err = None
            except Exception as e:
                new_val, new_err = None, f"{type(e).__name__}: {e}"
            if old_err or new_err:
                if old_err != new_err or (old_err is None) != (new_err is None):
                    mismatches += 1
                    print(f"  [ERR] {name} {m}{args}: old={old_err or old_val!r} new={new_err or new_val!r}")
                continue
            if type(old_val) is not type(new_val) or old_val != new_val:
                mismatches += 1
                print(f"  [DIFF] {name} {m}{args}: old={old_val!r} new={new_val!r}")
    print(f"variant[{name}]: checked={checked} mismatches={mismatches}")
    return mismatches


def main():
    backup = "/opt/okx_quant/strategies/futures_hedge.py.bak_calc_split_20260914_131631"
    old_methods = load_old_methods(backup)
    missing = [m for m in METHODS if m not in old_methods]
    if missing:
        print("MISSING old methods:", missing)
        return 1
    print("loaded old methods:", len(old_methods))
    _orig_time = time.time
    time.time = lambda: FIXED_NOW
    try:
        total = 0
        for name, builder in VARIANTS.items():
            total += run_variant(name, builder, old_methods)
    finally:
        time.time = _orig_time
    if total == 0:
        print("RESULT: PASS — 全部方法在全部场景下新旧结果一致")
        return 0
    print(f"RESULT: FAIL — {total} mismatches")
    return 1


if __name__ == "__main__":
    sys.exit(main())
