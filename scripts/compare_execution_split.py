# -*- coding: utf-8 -*-
"""拆分第五阶段影子对比：执行适配（旧实现 vs 新代理）。

- 旧实现：从备份 futures_hedge.py.bak_calc_split_20260914_131631 提取方法体，
  编译为 old_<name>(self, ...)，内部对已转换方法的 self 调用改写为旧实现
  （如 _place_open_order 内调 _submit_order → old 版本；_order_succeeded 内调
  _extract_order_id → old 版本）。
- 新实现：当前 futures_hedge.py 中的代理方法（指向 strategies.hedge_execution）。
- 执行方法有状态写入（gateway 调用记录、预算 token 提交/释放），旧/新分别在
  两个独立构建的实例上运行，消除状态串扰造成的假差异。
"""
import ast
import math
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, "/opt/okx_quant")
from strategies.futures_hedge import FuturesHedgeStrategy  # noqa: E402
from utils.helpers import safe_float  # noqa: E402
from execution.gateway import ExecutionGateway  # noqa: E402

METHODS = [
    "_submit_order", "_place_open_order", "_cancel_order", "_set_leverage",
    "_order_error_code", "_extract_order_id", "_order_succeeded",
]

_OLD_NAMES = {m: "old_" + m for m in METHODS}


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
    g = {"time": time, "safe_float": safe_float, "math": math, "ExecutionGateway": ExecutionGateway}
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


class FakeGateway:
    def __init__(self, place_result=None, cancel_result=True, lev_result=None, raise_cancel=False):
        self.place_result = place_result if place_result is not None else {"sCode": "0", "ordId": "ord-1"}
        self.cancel_result = cancel_result
        self.lev_result = lev_result if lev_result is not None else {"code": "0"}
        self.raise_cancel = raise_cancel
        self.calls = []
        self.cancels = []
        self.levs = []

    def place_order(self, symbol, side, qty, **kwargs):
        self.calls.append((symbol, side, qty, kwargs))
        return dict(self.place_result)

    def cancel_order(self, inst_id, ord_id):
        self.cancels.append((inst_id, ord_id))
        if self.raise_cancel:
            raise RuntimeError("cancel boom")
        return self.cancel_result

    def set_leverage(self, symbol, lever, margin_mode):
        self.levs.append((symbol, lever, margin_mode))
        return dict(self.lev_result)


class FakeAllocator:
    def __init__(self, reserve_token="tok-1", remaining=500.0):
        self.reserve_token = reserve_token
        self.remaining = remaining
        self.reserved = []
        self.committed = []
        self.released = []

    def reserve(self, margin, symbol, kind):
        self.reserved.append((margin, symbol, kind))
        return self.reserve_token

    def commit(self, token):
        self.committed.append(token)

    def release(self, token):
        self.released.append(token)

    def remaining_margin(self):
        return self.remaining

    def snapshot(self):
        return {"enabled": True, "remaining_margin": self.remaining}


def base_state():
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s.log = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    s._symbol = "BTC-USDT-SWAP"
    s._margin_mode = "cross"
    s._last_price = 0.125
    s._client = object()
    s._execution_gateway = FakeGateway()
    s._account_budget_allocator = None
    s._opening_order_block_reason = lambda side, qty, px, **kw: ""
    s._order_margin_required = lambda qty, price: 1.25
    s._risk_context_snapshot = lambda: {"account": {}}
    s._log_opening_guard_block = lambda *a, **k: None
    s._profile_slug = "test"
    s._opening_guard_events = []
    s._opening_guard_log_state = {}
    s._invalidate_tp_side = None
    return s


def v_guard_blocked():
    s = base_state()
    s._opening_order_block_reason = lambda side, qty, px, **kw: "max_side_notional"
    return s


def v_budget_reserve_ok():
    s = base_state()
    s._account_budget_allocator = FakeAllocator()
    return s


def v_budget_reserve_fail():
    s = base_state()
    s._account_budget_allocator = FakeAllocator(reserve_token="")
    return s


def v_budget_release():
    s = base_state()
    s._account_budget_allocator = FakeAllocator()
    s._execution_gateway = FakeGateway(place_result={"sCode": "51000", "data": []})
    return s


def v_reduce_only_success():
    s = base_state()
    return s


def v_reduce_only_51169():
    s = base_state()
    s._execution_gateway = FakeGateway(place_result={"sCode": "51169", "data": []})
    s._invalidate_tp_side = lambda side, **kw: None
    return s


def v_no_gateway():
    s = base_state()
    s._execution_gateway = None
    s._client = None
    return s


def v_cancel_ok():
    s = base_state()
    return s


def v_cancel_fail():
    s = base_state()
    s._execution_gateway = FakeGateway(cancel_result=False)
    return s


def v_cancel_exception():
    s = base_state()
    s._execution_gateway = FakeGateway(raise_cancel=True)
    return s


def v_leverage_ok():
    s = base_state()
    return s


def v_leverage_fail():
    s = base_state()
    s._execution_gateway = FakeGateway(lev_result={"code": "51000"})
    return s


VARIANTS = {
    "normal_open": base_state,
    "guard_blocked": v_guard_blocked,
    "budget_reserve_ok": v_budget_reserve_ok,
    "budget_reserve_fail": v_budget_reserve_fail,
    "budget_release": v_budget_release,
    "reduce_only_success": v_reduce_only_success,
    "reduce_only_51169": v_reduce_only_51169,
    "no_gateway": v_no_gateway,
    "cancel_ok": v_cancel_ok,
    "cancel_fail": v_cancel_fail,
    "cancel_exception": v_cancel_exception,
    "leverage_ok": v_leverage_ok,
    "leverage_fail": v_leverage_fail,
}

_PARSER_RESULTS = [
    {},
    {"sCode": "0", "ordId": "ord-9"},
    {"code": "0", "data": [{"ordId": "ord-inner", "sCode": "0"}]},
    {"sCode": "51000", "data": []},
    {"code": "0"},
    {"data": {"ordId": "ord-dict"}},
    {"data": [{"errCode": "51169"}]},
    {"data": [{"code": "0", "ordId": "a"}, {"sCode": "x"}]},
    {"sCode": "", "ordId": " "},
    "not-a-dict",
    None,
]

ARG_MATRIX = {
    "_submit_order": [
        ("BTC-USDT-SWAP", "buy", 10),
        ("BTC-USDT-SWAP", "buy", 10, "limit", 0.125, "long"),
        ("BTC-USDT-SWAP", "buy", 10, "limit", 0.125, "long", False, "USDT"),
        ("BTC-USDT-SWAP", "buy", 10, "market", None, "long", False, "", None, True),
        ("BTC-USDT-SWAP", "buy", 10, "market", None, "long", False, "", None, False, True),
        ("BTC-USDT-SWAP", "sell", 5, "market", None, "short", True),
        ("BTC-USDT-SWAP", "sell", 5, "market", None, "short", True, "", None, False, False, True),
        ("BTC-USDT-SWAP", "buy", 0),
    ],
    "_place_open_order": [
        ("long", 10, "long"),
        ("short", 5, "short", "limit", 0.125),
        ("long", 10, "long", "market", None, {"long": 2, "short": 0}, True),
    ],
    "_cancel_order": [
        ("ord-1",),
        ("ord-1", "BTC-USDT-SWAP"),
        ("",),
    ],
    "_set_leverage": [
        ("BTC-USDT-SWAP", 5),
        ("BTC-USDT-SWAP", 10, "cross"),
    ],
    "_order_error_code": [(r,) for r in _PARSER_RESULTS],
    "_extract_order_id": [(r,) for r in _PARSER_RESULTS],
    "_order_succeeded": [(r,) for r in _PARSER_RESULTS],
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
    total = 0
    for name, builder in VARIANTS.items():
        total += run_variant(name, builder, old_methods)
    if total == 0:
        print("RESULT: PASS — 全部方法在全部场景下新旧结果一致")
        return 0
    print(f"RESULT: FAIL — {total} mismatches")
    return 1


if __name__ == "__main__":
    sys.exit(main())
