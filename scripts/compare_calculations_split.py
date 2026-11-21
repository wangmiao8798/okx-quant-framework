# -*- coding: utf-8 -*-
"""拆分第二阶段影子对比：纯计算方法（旧实现 vs 新代理）。

- 旧实现：从备份 futures_hedge.py.bak_status_split_20260914_113828 提取方法体，
  编译为 old_<name>(self, ...)，内部对已转换方法的 self 调用也改写为旧实现，
  保证逐方法严格对比。
- 新实现：当前 futures_hedge.py 中的代理方法（指向 strategies.hedge_calculations）。
- 同一策略实例、同一参数矩阵下对比，结果必须完全一致。
"""
import ast
import math
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/opt/okx_quant")
from strategies.futures_hedge import FuturesHedgeStrategy, HedgeInventory, Pos  # noqa: E402
from utils.helpers import safe_float  # noqa: E402

METHODS = [
    "_daily_drawdown_ratio", "_side_value", "_side_float", "_side_int",
    "_effective_base_pos_usdt", "_effective_side_step", "_effective_side_multiplier",
    "_effective_side_max_adds", "_effective_side_tp_ratio", "_effective_add_cooldown",
    "_effective_drift_threshold", "_net_imbalance_ratio", "_dominant_side", "_calc_pnl",
    "_effective_tp_ratio", "_effective_max_side_notional_usdt", "_position_age_sec",
    "_position_unrealized_pnl", "_round_tp_price_away",
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


def base_state(s):
    # 公共属性（两个场景都需要的计算输入）
    s._side_config = {"long": {"add_step": 0.004, "max_add_layers": 4}, "short": {}}
    s._add_step = 0.003
    s._add_mult = 1.25
    s._max_adds = 6
    s._first_tp = 0.0065
    s._final_tp = 0.013
    s._trail = 0.015
    s._min_edge_ratio = 0.0035
    s._cooldown = 120.0
    s._drift_threshold = 0.01
    s._moca_state = "calm"
    s._base_pos_usdt = 50.0
    s._recovery_pos_multiplier = 0.5
    s._position_first_seen = {"long": time.time() - 3600, "short": time.time() - 1800}
    s._last_price = 0.1258
    s._tick_size = 0.0001
    s._min_qty = 10.0
    s._inventory = HedgeInventory()
    s._inventory.long = Pos("long", 120.0, 0.1245, 0.0, 0)
    s._inventory.short = Pos("short", 95.0, 0.1251, 0.0, 0)
    s._long = s._inventory.long
    s._short = s._inventory.short
    s._day_equity_baseline = 1000.0
    s._cached_equity = 987.0
    s._margin_first_capacity_enabled = False
    s._margin_first_normal_margin_ratio = 0.5
    s._margin_first_max_margin_ratio = 0.6
    s._margin_first_reduced_size_ratio = 0.5
    s._account_margin_load_ratio = lambda: 0.31
    s._in_recovery_mode = lambda: False
    s._dynamic_notional_limits = lambda: {
        "side_cap": 150.0, "long_cap": 150.0, "short_cap": 150.0, "total_cap": 300.0,
    }
    return s


def build_normal():
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s = base_state(s)
    return s


def build_edge():
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s = base_state(s)
    # 边界：空/异常状态
    s._side_config = {"long": {}, "short": {"add_step": None, "max_add_layers": 0}}
    s._add_step = 0.0
    s._add_mult = 0.0
    s._max_adds = 0
    s._first_tp = 0.0
    s._final_tp = 0.0
    s._trail = 0.0
    s._min_edge_ratio = 0.0
    s._cooldown = 0.0
    s._drift_threshold = 0.0
    s._moca_state = "spike"
    s._base_pos_usdt = 0.0
    s._recovery_pos_multiplier = 0.0
    s._position_first_seen = None
    s._last_price = 0.0
    s._tick_size = 0.0
    s._min_qty = 0.0
    s._inventory.long = Pos("long", 0.0, 0.0, 0.0, 0)
    s._inventory.short = Pos("short", 0.0, 0.0, 0.0, 0)
    s._long = s._inventory.long
    s._short = s._inventory.short
    s._day_equity_baseline = 0.0
    s._cached_equity = 0.0
    s._margin_first_capacity_enabled = True
    s._margin_first_normal_margin_ratio = 0.5
    s._margin_first_max_margin_ratio = 0.6
    s._margin_first_reduced_size_ratio = 0.5
    s._account_margin_load_ratio = lambda: 0.55
    s._in_recovery_mode = lambda: True
    s._dynamic_notional_limits = lambda: {"side_cap": 0.0, "long_cap": 0.0, "short_cap": 0.0, "total_cap": 0.0}
    return s


ARG_MATRIX = {
    "_side_value": [("long", "add_step", 1.0), ("long", "max_add_layers", 5), ("short", "add_step", 2.0),
                    ("short", "max_add_layers", 3), ("", "add_step", 9.0), ("LONG", "add_step", 7.0),
                    ("long", "missing_key", None), ("long", "missing_key", "fallback"), ("middle", "x", 0.0)],
    "_side_float": [("long", "add_step", 1.0), ("short", "add_step", 2.0), ("", "add_step", 9.0),
                    ("long", "missing", 3.5), ("LONG", "max_add_layers", 4.0)],
    "_side_int": [("long", "add_step", 1), ("short", "add_step", 2), ("", "add_step", 9),
                  ("long", "max_add_layers", 4), ("long", "missing", 3), ("short", "max_add_layers", 0)],
    "_effective_base_pos_usdt": [(), ("long",), ("short",), ("",)],
    "_effective_side_step": [("long",), ("short",), ("",)],
    "_effective_side_multiplier": [("long",), ("short",), ("",)],
    "_effective_side_max_adds": [("long",), ("short",), ("",)],
    "_effective_side_tp_ratio": [("long", "first"), ("long", "final"), ("short", "first"), ("short", "final"),
                                 ("short", "trailing"), ("", "first"), ("long", "other"), ("LONG", "final")],
    "_effective_add_cooldown": [()],
    "_effective_drift_threshold": [()],
    "_net_imbalance_ratio": [()],
    "_dominant_side": [()],
    "_calc_pnl": [(0.0,), (0.1245,), (0.1258,), (0.13,), (1.0,), (100.0,)],
    "_effective_tp_ratio": [(0.01, "maker"), (0.01, "taker"), (0.0001, "maker"), (0.0, "taker"),
                            (0.1, "other"), (0.0035, "maker")],
    "_effective_max_side_notional_usdt": [(), ("long",), ("short",), ("x",)],
    "_position_age_sec": [("long",), ("short",), ("middle",), ("",)],
    "_position_unrealized_pnl": [("long",), ("short",), ("middle",), ("",)],
    "_round_tp_price_away": [("long", 0.12345), ("long", 0.1239), ("short", 0.12345), ("short", 0.1239),
                             ("long", 1.0), ("long", 0.0), ("short", -0.5), ("long", 0.12580001)],
    "_daily_drawdown_ratio": [()],
}


def run_scenario(name, state, old_methods):
    fixed_now = 1757800000.0
    _orig_time = time.time
    time.time = lambda: fixed_now
    try:
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
                        print(f"  [ERR] {m}{args}: old={old_err or old_val!r} new={new_err or new_val!r}")
                    continue
                if type(old_val) is not type(new_val) or old_val != new_val:
                    mismatches += 1
                    print(f"  [DIFF] {m}{args}: old={old_val!r} new={new_val!r}")
        print(f"scenario[{name}]: checked={checked} mismatches={mismatches}")
        return mismatches
    finally:
        time.time = _orig_time


def main():
    backup = "/opt/okx_quant/strategies/futures_hedge.py.bak_status_split_20260914_113828"
    old_methods = load_old_methods(backup)
    missing = [m for m in METHODS if m not in old_methods]
    if missing:
        print("MISSING old methods:", missing)
        return 1
    print("loaded old methods:", len(old_methods))
    total = 0
    total += run_scenario("normal", build_normal(), old_methods)
    total += run_scenario("edge", build_edge(), old_methods)
    if total == 0:
        print("RESULT: PASS — 全部方法在全部场景下新旧结果一致")
        return 0
    print(f"RESULT: FAIL — {total} mismatches")
    return 1


if __name__ == "__main__":
    sys.exit(main())
