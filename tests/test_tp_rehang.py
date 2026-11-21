# -*- coding: utf-8 -*-
"""Tests for the TP-rehang path (`_rehang_far_tps`).

The rehang must never:
  * act while disabled or in dry-run mode;
  * touch a TP that is already close to market;
  * manufacture an instant fill;
  * push projected net exposure past the configured hard cap.
"""
import types

import pytest

from strategies.hedge_kit.tp import TpMixin


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg):
        self.lines.append(("info", str(msg)))

    def warning(self, msg):
        self.lines.append(("warn", str(msg)))

    def error(self, msg):
        self.lines.append(("error", str(msg)))


class _Side:
    def __init__(self, qty=0.0, entry=0.0):
        self.qty = qty
        self.entry = entry


class _Fake(TpMixin):
    """Minimal host exposing only what _rehang_far_tps touches."""

    MAKER_FEE = 0.0002
    TAKER_FEE = 0.0005

    def __init__(self, long_qty=0.0, long_entry=0.0, short_qty=0.0, short_entry=0.0,
                 ct_val=1.0, net_hard=150.0, enabled=True, dry=False, min_gap=0.02,
                 net_soft=0.0):
        self.log = _Log()
        self._long = _Side(long_qty, long_entry)
        self._short = _Side(short_qty, short_entry)
        self._layer_tps = {}
        self._layer_qty_map = {}
        self._layer_side_map = {}
        self._layer_tp_orders = {}
        self._tp_rehang_enabled = enabled
        self._tp_rehang_dry_run = dry
        self._tp_rehang_min_gap_ratio = min_gap
        self._tp_rehang_step_ratio = 0.5
        self._tp_rehang_cooldown_sec = 900.0
        self._tp_rehang_max_times = 4
        self._tp_rehang_net_headroom = 0.8
        self._tp_rehang_state = {}
        self._max_net_notional_usdt = net_hard
        self._max_net_notional_soft_usdt = net_soft
        self._ct_val = ct_val
        self._last_tp_place_failure = {}
        self.cancelled = []
        self.placed = []

    # --- host primitives used by the rehang path -------------------------
    def _tp_lid_side(self, lid):
        return self._layer_side_map.get(lid, "")

    def _round_tp_price_away(self, side, price):
        return round(price, 5)

    def _minimum_profitable_tp_price(self, side, qty, entry):
        # entry +/- 0.2% covers fees for the purposes of these tests
        return entry * 1.002 if side == "long" else entry * 0.998

    def _cancel_tp_order(self, lid):
        self.cancelled.append(lid)

    def _place_tp_order(self, lid, side, qty, tp_price, coverage_safety=False, exclude_qty=0.0):
        self.placed.append((lid, side, qty, tp_price))
        return True

    def add_layer(self, lid, side, tp, qty):
        self._layer_tps[lid] = tp
        self._layer_side_map[lid] = side
        self._layer_qty_map[lid] = qty


def run(fake, price):
    TpMixin._rehang_far_tps(fake, price)


def test_disabled_does_nothing():
    f = _Fake(long_qty=100, long_entry=1.0, enabled=False)
    f.add_layer("L1", "long", 1.5, 100)
    run(f, 1.0)
    assert f.placed == [] and f.cancelled == []


def test_dry_run_only_logs():
    f = _Fake(long_qty=100, long_entry=1.0, dry=True)
    f.add_layer("L1", "long", 1.5, 100)
    run(f, 1.0)
    assert f.placed == [] and f.cancelled == []
    assert any("dry-run" in m for _, m in f.log.lines)


def test_close_tp_untouched():
    f = _Fake(long_qty=100, long_entry=1.0)
    f.add_layer("L1", "long", 1.005, 100)  # only 0.5% away, below min_gap
    run(f, 1.0)
    assert f.placed == [] and f.cancelled == []


def test_rehang_moves_half_way_and_keeps_distance():
    # short 0.62 @ 2399.57, TP stranded at 2327.7, market 2396.24
    f = _Fake(short_qty=0.62, short_entry=2399.57, long_qty=0.62, long_entry=2709.26,
              ct_val=0.1, net_hard=150.0)
    f.add_layer("S1", "short", 2327.7, 0.31)
    run(f, 2396.24)
    assert len(f.placed) == 1
    _, side, qty, new_tp = f.placed[0]
    assert side == "short"
    assert 2327.7 < new_tp < 2396.24
    # must stay strictly below market so it cannot fill instantly
    assert new_tp < 2396.24 * 0.999


def test_net_exposure_guard_blocks_crv_long():
    # CRV long 2197 @ 0.345756 vs short 2174 -> freeing the long leg leaves ~-392U net
    f = _Fake(long_qty=2197, long_entry=0.345756, short_qty=2174, short_entry=0.330431,
              ct_val=1.0, net_hard=150.0)
    f.add_layer("L9", "long", 0.405, 1098)
    run(f, 0.3651)
    assert f.placed == [] and f.cancelled == []
    assert any("net_after" in m for _, m in f.log.lines)


def test_no_instant_fill_is_forced():
    # long whose cost already sits below market: target would fill immediately
    f = _Fake(long_qty=100, long_entry=0.9, short_qty=0.0, ct_val=1.0, net_hard=10000.0)
    f.add_layer("L1", "long", 1.30, 50)
    run(f, 1.0)
    assert len(f.placed) == 1
    _, _, _, new_tp = f.placed[0]
    assert new_tp > 1.0 * 1.001


def test_max_times_caps_rehangs():
    f = _Fake(long_qty=100, long_entry=0.9, ct_val=1.0, net_hard=10000.0)
    f.add_layer("L1", "long", 1.30, 50)
    f._tp_rehang_state = {"L1": {"n": 4, "ts": 0.0, "last": 1.30}}
    run(f, 1.0)
    assert f.placed == []


def test_flat_position_skipped():
    f = _Fake(long_qty=0.0, long_entry=0.0)
    f.add_layer("L1", "long", 1.5, 100)
    run(f, 1.0)
    assert f.placed == []


def test_net_guard_falls_back_to_soft_when_hard_unset():
    # hard cap missing, soft cap 100 -> guard must still block the CRV-sized leg
    f = _Fake(long_qty=2197, long_entry=0.345756, short_qty=2174, short_entry=0.330431,
              ct_val=1.0, net_hard=0.0, net_soft=100.0)
    f.add_layer("L9", "long", 0.405, 1098)
    run(f, 0.3651)
    assert f.placed == []
    assert any("net_after" in m for _, m in f.log.lines)


def test_net_guard_refuses_when_no_cap_configured():
    # neither hard nor soft configured -> do nothing rather than fly blind
    f = _Fake(long_qty=100, long_entry=0.9, ct_val=1.0, net_hard=0.0, net_soft=0.0)
    f.add_layer("L1", "long", 1.30, 50)
    run(f, 1.0)
    assert f.placed == [] and f.cancelled == []
