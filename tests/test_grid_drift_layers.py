# -*- coding: utf-8 -*-
"""Tests for `_check_drift` layer-index repositioning.

A live grid order only needs repositioning when it can no longer serve the
grid: beyond the outermost allowed layer, or on the wrong side of price.
Anchoring the check to the nearest target price instead made every small tick
look like drift, so the ladder was rebuilt before any order could fill.

Single-sided cases are used when measuring the tolerance band, because a price
move large enough to exceed the old band also crosses the nearest opposite
order -- and that crossing is a fill, not drift.
"""
from strategies.futures_hedge import FuturesHedgeStrategy


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append(str(msg))

    def error(self, msg):
        self.lines.append(str(msg))


def _host(price, long_levels=(), short_levels=(), max_gl=2, step=0.00159,
          drift_threshold=0.02, center=None):
    obj = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    obj.log = _Log()
    obj._grid_center_price = price if center is None else center
    obj._last_price = price
    obj._long_grid_levels = list(long_levels)
    obj._short_grid_levels = list(short_levels)
    obj._moca_state = "calm"
    obj._drift_threshold = drift_threshold
    obj._order_refresh_tolerance_pct = 0.005
    obj._in_startup_recovery = lambda: False
    obj._in_post_recovery_refill_delay = lambda: False
    obj._target_grid_layers = lambda: max_gl
    obj._effective_side_step = lambda side: step
    obj._effective_drift_threshold = lambda: drift_threshold
    obj._rebuild_grid = lambda: "rebuilt"
    return obj


def _levels(price, max_gl, step, side):
    sign = -1.0 if side == "long" else 1.0
    return [price * (1 + sign * n * step) for n in range(1, max_gl + 1)]


def test_no_rebuild_when_levels_on_target():
    price, step, max_gl = 100.0, 0.00159, 2
    obj = _host(price,
                long_levels=_levels(price, max_gl, step, "long"),
                short_levels=_levels(price, max_gl, step, "short"),
                max_gl=max_gl, step=step)
    assert obj._check_drift() is None


def test_small_move_keeps_ladder_alive():
    """0.3% used to exceed the old 1.57x-step band and rebuilt the ladder."""
    price, step, max_gl = 100.0, 0.00159, 2
    long_levels = _levels(price, max_gl, step, "long")
    moved = price * 1.003
    obj = _host(moved, long_levels=long_levels, max_gl=max_gl, step=step)
    assert obj._check_drift() is None


def test_far_move_rebuilds():
    price, step, max_gl = 100.0, 0.00159, 2
    long_levels = _levels(price, max_gl, step, "long")
    obj = _host(price * 1.02, long_levels=long_levels, max_gl=max_gl, step=step)
    assert obj._check_drift() == "rebuilt"


def test_long_buy_above_price_is_stale():
    """A buy level above price means it should already have filled."""
    price, step = 100.0, 0.00159
    obj = _host(price, long_levels=[price * 1.001], max_gl=2, step=step)
    assert obj._check_drift() == "rebuilt"


def test_short_sell_below_price_is_stale():
    price, step = 100.0, 0.00159
    obj = _host(price, short_levels=[price * 0.999], max_gl=2, step=step)
    assert obj._check_drift() == "rebuilt"


def test_three_layer_grid_tolerates_two_layer_drift():
    price, step, max_gl = 100.0, 0.0035, 3
    long_levels = _levels(price, max_gl, step, "long")
    obj = _host(price * 1.005, long_levels=long_levels, max_gl=max_gl, step=step)
    assert obj._check_drift() is None


def test_drift_threshold_still_triggers_rebuild():
    price, step = 100.0, 0.00159
    obj = _host(price * 1.05, max_gl=2, step=step,
                drift_threshold=0.02, center=price)
    assert obj._check_drift() == "rebuilt"


def test_empty_ladder_does_not_rebuild():
    obj = _host(100.0, max_gl=2)
    assert obj._check_drift() is None
