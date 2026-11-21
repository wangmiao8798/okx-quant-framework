"""固化「运行时止盈覆盖自愈」(_heal_missing_tp_coverage) 的行为。

背景（2026-11 反复事故）：某边 TP 订单在运行时消失（撤单/成交/交易所抖动，
或 stale 交易所覆盖缓存骗过 _supplement_tp_coverage_side 以为已覆盖）后，旧逻辑
只能在启动 [initTP] 或 sync-recovery 分支重建，导致该边长期裸露、tp_coverage_incomplete
永久封锁加仓，只能等重启止血。

_heal_missing_tp_coverage 作为主循环里的独立定时器：拉交易所活单刷新增量覆盖缓存，
对「有持仓但活单止盈≈0」的边调用 _supplement_tp_coverage_side 重建——基于交易所真实
活单（非本地 _layer_qty_map），连幽灵层场景也能识别自愈。
"""
import types

import pytest

from strategies.hedge_kit.tp import TpMixin


class _HealLog:
    def __init__(self):
        self.warnings = []

    def info(self, *a, **k):
        pass

    def warning(self, msg, *a, **k):
        self.warnings.append(str(msg))

    def error(self, *a, **k):
        pass


class _Pos:
    def __init__(self, qty, entry):
        self.qty = qty
        self.entry = entry


def _make(short_qty=0.0, long_qty=0.0, live_orders=None, *, startup=False,
          heal_interval=0.0, last_heal=0.0):
    """构造可运行 _heal_missing_tp_coverage 的最小替身。"""
    s = types.SimpleNamespace()
    s.log = _HealLog()
    s._symbol = "RAM-USDT-SWAP"
    s._client = object()  # truthy
    s._last_tp_heal = last_heal
    s._tp_heal_interval = heal_interval
    s._long = _Pos(long_qty, entry=8.0 if long_qty > 0 else 0.0)
    s._short = _Pos(short_qty, entry=9.0 if short_qty > 0 else 0.0)
    s._exchange_tp_coverage_ready = False
    s._exchange_tp_coverage_qty = {"long": 0.0, "short": 0.0}
    s._in_startup_recovery = lambda: startup
    s._deduplicate_live_tp_orders = lambda orders: orders
    s._fetch_live_orders = lambda: list(live_orders or [])
    s._tp_coverage_from_orders = TpMixin._tp_coverage_from_orders

    def _update(orders):
        cov = s._tp_coverage_from_orders(orders)
        s._exchange_tp_coverage_qty = cov
        s._exchange_tp_coverage_ready = True
        return cov

    s._update_exchange_tp_coverage = _update
    s._risk_tp_covered_qty = TpMixin._risk_tp_covered_qty.__get__(s)
    s._position_qty_tolerance = lambda: 1e-6
    s._tp_coverage_tolerance = lambda side, qty=0.0: 1e-6
    s._supplement_calls = []

    def _supplement(side, pos):
        s._supplement_calls.append(side)
        return 1

    s._supplement_tp_coverage_side = _supplement
    s._heal_missing_tp_coverage = types.MethodType(TpMixin._heal_missing_tp_coverage, s)
    return s


def _long_order(sz):
    return {"reduceOnly": "true", "posSide": "long", "side": "sell",
            "px": "8.5", "sz": str(sz), "ordId": "ol"}


def _short_order(sz):
    return {"reduceOnly": "true", "posSide": "short", "side": "buy",
            "px": "9.1", "sz": str(sz), "ordId": "os"}


def test_heals_only_uncovered_short_side():
    """空单持仓、交易所无空单活单止盈 -> 仅触发空单补充；多单有活单则不动。"""
    s = _make(short_qty=69.0, long_qty=148.6,
              live_orders=[_long_order(148.6)])  # 多单覆盖, 空单缺失
    s._heal_missing_tp_coverage()
    assert s._supplement_calls == ["short"], s._supplement_calls


def test_no_heal_when_both_sides_covered():
    """双边都有交易所活单止盈 -> 不触发任何补充。"""
    s = _make(short_qty=69.0, long_qty=148.6,
              live_orders=[_long_order(148.6), _short_order(69.0)])
    s._heal_missing_tp_coverage()
    assert s._supplement_calls == [], s._supplement_calls


def test_skip_during_startup_recovery():
    """启动恢复期内即使缺失也跳过，避免干扰初始化。"""
    s = _make(short_qty=69.0, long_qty=148.6, live_orders=[], startup=True)
    s._heal_missing_tp_coverage()
    assert s._supplement_calls == [], s._supplement_calls


def test_throttled_by_interval():
    """定时器未到间隔 -> 跳过（用极大 last_heal 模拟刚跑过）。"""
    import time
    s = _make(short_qty=69.0, long_qty=148.6, live_orders=[],
              heal_interval=120.0, last_heal=time.time())
    s._heal_missing_tp_coverage()
    assert s._supplement_calls == [], s._supplement_calls


def test_heal_logs_rebuild():
    """缺失边重建时应输出 [TP-heal] 警告日志。"""
    s = _make(short_qty=69.0, long_qty=0.0, live_orders=[])
    s._heal_missing_tp_coverage()
    assert "short" in s._supplement_calls
    assert any("TP-heal" in w and "RAM-USDT-SWAP" in w and "short" in w
               for w in s.log.warnings)
