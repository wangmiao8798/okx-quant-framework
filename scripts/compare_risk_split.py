# -*- coding: utf-8 -*-
"""拆分第三阶段影子对比：风控决策方法（旧实现 vs 新代理）。

- 旧实现：从备份 futures_hedge.py.bak_calc_split_20260914_131631 提取方法体，
  编译为 old_<name>(self, ...)，内部对已转换方法的 self 调用也改写为旧实现
  （如 _grid_capacity_qty 内部调 _opening_order_block_reason → old 版本），
  保证逐方法严格对比。
- 新实现：当前 futures_hedge.py 中的代理方法（指向 strategies.hedge_risk）。
- 同一策略实例、同一参数矩阵、多种状态变体下对比，结果必须完全一致。
"""
import ast
import math
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/opt/okx_quant")
from strategies.futures_hedge import FuturesHedgeStrategy, Pos  # noqa: E402
from utils.helpers import safe_float  # noqa: E402
from risk.account_budget import order_margin_requirement  # noqa: E402
from risk.position_limits import (  # noqa: E402
    opening_limits_block_reason,
    net_exposure_ratio,
    net_aware_margin_stop_ratio,
)

METHODS = [
    "_opening_order_block_reason", "_grid_capacity_qty",
    "_account_budget_block_reason", "_order_margin_required",
    "_mgn_ratio_opening_block_reason", "_inventory_margin_thresholds",
    "_inventory_repair_plan", "_inventory_repair_cooldown",
]

_OLD_NAMES = {m: "old_" + m for m in METHODS}


class _RewriteSelfCalls(ast.NodeTransformer):
    """把方法体内对已转换方法的 self.<m>(...) 改写为 old_<m>(self, ...)。"""

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
    g = {
        "time": time,
        "safe_float": safe_float,
        "math": math,
        "order_margin_requirement": order_margin_requirement,
        "opening_limits_block_reason": opening_limits_block_reason,
        "net_exposure_ratio": net_exposure_ratio,
        "net_aware_margin_stop_ratio": net_aware_margin_stop_ratio,
    }
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


class FakeAllocator:
    def __init__(self, remaining, enabled=True):
        self._remaining = remaining
        self._enabled = enabled

    def remaining_margin(self):
        return self._remaining

    def snapshot(self):
        return {"enabled": self._enabled, "remaining_margin": self._remaining}


class FakeInventory:
    def __init__(self, plan):
        self._plan = plan

    def repair_plan(self, *args, **kwargs):
        return dict(self._plan)


def _pos(qty):
    return Pos("long" if qty >= 0 else "short", abs(qty), 0.125, 0.0, 0)


def base_state():
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s._reduce_only_mode = False
    s._risk_opening_pause_active = lambda: False
    s._risk_opening_pause_reason = ""
    s._instrument_info_ready = True
    s._has_leverage_mismatch = lambda: False
    s._client = None
    s._fetch_live_orders = lambda: []
    s._tp_coverage_block_reason = lambda side: ""
    s._pending_opening_margin = lambda orders=None: 12.0
    s._dynamic_notional_limits = lambda: {
        "side_cap": 150.0, "long_cap": 150.0, "short_cap": 150.0, "total_cap": 300.0,
    }
    s._margin_first_capacity_ready = lambda: False
    s._account_margin_load_ratio = lambda: 0.31
    s._account_margin_data_block_reason = lambda: ""
    s._account_mgn_ratio = lambda: 8.5
    s._mgn_ratio_open_limit = 5.0
    s._mgn_ratio_emergency_limit = 2.0
    s._mgn_ratio_pause_limit = 3.0
    s._last_price = 0.1258
    s._ct_val = 1.0
    s._cached_equity = 987.0
    s._working_total_notional_ratio = 1.0
    s._max_net_notional_usdt = 60.0
    s._inventory_repair_side_cap_multiplier = 1.10
    s._inventory_repair_max_margin_ratio = 0.35
    s._inventory_margin_stop_ratio = 0.35
    s._inventory_margin_resume_ratio = 0.30
    s._inventory_margin_hedge_credit_ratio = 0.20
    s._max_margin_ratio = 0.45
    s._account_margin_soft_limit = 0.50
    s._inventory_repair_imbalance_gate = 0.70
    s._inventory_repair_stage_ratio = 0.30
    s._inventory_repair_mode = False
    s._inventory_repair_exit_net_notional_usdt = 48.0
    s._inventory_repair_exit_imbalance_ratio = 0.45
    s._inventory_repair_fast_cooldown_sec = 30.0
    s._inventory_repair_medium_cooldown_sec = 60.0
    s._inventory_repair_cooldown_sec = 120.0
    s._cur_lev = 5.0
    s._margin_buffer_ratio = 0.10
    s._qty_step = 0.1
    s._min_qty = 1.0
    s._account_budget_allocator = None
    s._inventory = FakeInventory({
        "active": True, "stage": 1, "side": "short", "qty": 5.0,
    })
    s._long = _pos(120.0)
    s._short = _pos(95.0)
    return s


def variant_reduce_only():
    s = base_state()
    s._reduce_only_mode = True
    return s


def variant_paused():
    s = base_state()
    s._risk_opening_pause_active = lambda: True
    s._risk_opening_pause_reason = "manual_pause"
    return s


def variant_no_instrument():
    s = base_state()
    s._instrument_info_ready = False
    return s


def variant_lev_mismatch():
    s = base_state()
    s._has_leverage_mismatch = lambda: True
    return s


def variant_tp_block():
    s = base_state()
    s._tp_coverage_block_reason = lambda side: "tp_uncovered"
    return s


def variant_mgn_emergency():
    s = base_state()
    s._account_mgn_ratio = lambda: 1.5
    return s


def variant_mgn_pause():
    s = base_state()
    s._account_mgn_ratio = lambda: 2.5
    return s


def variant_mgn_limit():
    s = base_state()
    s._account_mgn_ratio = lambda: 3.5
    return s


def variant_mgn_data():
    s = base_state()
    s._account_margin_data_block_reason = lambda: "margin_data_stale"
    return s


def variant_budget_block():
    s = base_state()
    s._account_budget_allocator = FakeAllocator(remaining=5.0)
    return s


def variant_budget_ok():
    s = base_state()
    s._account_budget_allocator = FakeAllocator(remaining=500.0)
    return s


def variant_margin_first():
    s = base_state()
    s._margin_first_capacity_ready = lambda: True
    return s


def variant_repair():
    s = base_state()
    # 弱侧（short）加上修复乘数路径；net 多头主导
    s._long = _pos(180.0)
    s._short = _pos(20.0)
    return s


def variant_live_orders():
    s = base_state()
    s._client = object()
    s._fetch_live_orders = lambda: [
        {"posSide": "long", "sz": "5", "reduceOnly": "false"},
        {"posSide": "short", "sz": "3", "reduceOnly": "true"},
        {"posSide": "long", "sz": "2", "reduceOnly": "false"},
    ]
    return s


def variant_edge():
    s = base_state()
    s._long = _pos(0.0)
    s._short = _pos(0.0)
    s._cached_equity = 0.0
    s._dynamic_notional_limits = lambda: {
        "side_cap": 0.0, "long_cap": 0.0, "short_cap": 0.0, "total_cap": 0.0,
    }
    s._max_net_notional_usdt = 0.0
    s._qty_step = 0.0
    s._min_qty = 0.0
    s._last_price = 0.0
    s._ct_val = 0.0
    s._account_mgn_ratio = lambda: 0.0
    return s


VARIANTS = {
    "normal": base_state,
    "reduce_only": variant_reduce_only,
    "paused": variant_paused,
    "no_instrument": variant_no_instrument,
    "lev_mismatch": variant_lev_mismatch,
    "tp_block": variant_tp_block,
    "mgn_emergency": variant_mgn_emergency,
    "mgn_pause": variant_mgn_pause,
    "mgn_limit": variant_mgn_limit,
    "mgn_data": variant_mgn_data,
    "budget_block": variant_budget_block,
    "budget_ok": variant_budget_ok,
    "margin_first": variant_margin_first,
    "repair": variant_repair,
    "live_orders": variant_live_orders,
    "edge": variant_edge,
}

ARG_MATRIX = {
    "_opening_order_block_reason": [
        ("long", 10, 0.125),
        ("short", 10, 0.125),
        ("bad", 10, 0.125),
        ("long", 0, 0.125),
        ("long", 10, 0.0),
        ("long", -5, 0.125, {"long": 3, "short": 0}),
        ("long", 10, 0.125, {"long": 3, "short": 0}),
        ("long", 10, 0.125, None, True),
        ("long", 10, 0.125, {"long": 3, "short": 0}, False, True),
        ("long", 10, 0.125, {"long": 3, "short": 0}, False, False, True),
    ],
    "_grid_capacity_qty": [
        ("long", 30, 0.125),
        ("short", 30, 0.125),
        ("bad", 30, 0.125),
        ("long", 0, 0.125),
        ("long", 30, 0.0),
        ("long", 30, 0.125, {"long": 5, "short": 0}),
        ("long", 30, 0.125, None, True),
        ("short", 50, 0.125, None, True),
    ],
    "_account_budget_block_reason": [
        (10, 0.125),
        (10, 0.125, 5.0),
        (100, 0.125),
        (10, 0.125, -1.0),
    ],
    "_order_margin_required": [
        (10, 0.125),
        (0, 0.125),
        (10, 0.0),
        (-5, 0.125),
    ],
    "_mgn_ratio_opening_block_reason": [()],
    "_inventory_margin_thresholds": [()],
    "_inventory_repair_plan": [
        (0.125,),
        (0.125, True),
        (0.0,),
        (0.125, False),
    ],
    "_inventory_repair_cooldown": [
        (10.0,),
        (300.0,),
        (400.0,),
        (700.0,),
        (0.0,),
        (-10.0,),
    ],
}


def run_variant(name, state, old_methods):
    mismatches = 0
    checked = 0
    for m in METHODS:
        old_fn = old_methods.get(m)
        new_fn = getattr(state, m)
        for args in ARG_MATRIX.get(m, [()]):
            checked += 1
            try:
                old_val = old_fn(state, *args)
                old_err = None
            except Exception as e:
                old_val, old_err = None, f"{type(e).__name__}: {e}"
            try:
                new_val = new_fn(*args)
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
    total = 0
    for name, builder in VARIANTS.items():
        total += run_variant(name, builder(), old_methods)
    if total == 0:
        print("RESULT: PASS — 全部方法在全部场景下新旧结果一致")
        return 0
    print(f"RESULT: FAIL — {total} mismatches")
    return 1


if __name__ == "__main__":
    sys.exit(main())
