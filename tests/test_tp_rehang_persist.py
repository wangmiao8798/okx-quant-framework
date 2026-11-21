# -*- coding: utf-8 -*-
"""Persistence of the TP-rehang counters.

The counters used to live only in memory, so every restart handed the
strategy a fresh `max_times` budget and a fresh cooldown. A handful of
restarts could therefore walk a stranded TP all the way down to market.

These tests pin the contract:
  * counters survive a JSON round-trip through the PnL file;
  * a restored counter still caps rehangs (no budget refill on restart);
  * expired / malformed records are released instead of sticking forever;
  * dropping a layer's TP also drops its counter, so a reused lid starts clean.
"""
import json

import pytest

from strategies.hedge_kit.state import StateMixin
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


class _Fake(TpMixin, StateMixin):
    """Minimal host exposing only what the rehang path touches.

    StateMixin supplies the real payload serialiser; `_save_pnl_data` is
    stubbed below so the tests can observe what would be written to disk.
    """

    MAKER_FEE = 0.0002
    TAKER_FEE = 0.0005

    def __init__(self, long_qty=100.0, long_entry=0.9, short_qty=0.0, short_entry=0.0,
                 ct_val=1.0, net_hard=10000.0, ttl=86400.0):
        self.log = _Log()
        self._long = _Side(long_qty, long_entry)
        self._short = _Side(short_qty, short_entry)
        self._layer_tps = {}
        self._layer_qty_map = {}
        self._layer_side_map = {}
        self._layer_tp_orders = {}
        self._tp_rehang_enabled = True
        self._tp_rehang_dry_run = False
        self._tp_rehang_min_gap_ratio = 0.02
        self._tp_rehang_step_ratio = 0.5
        self._tp_rehang_cooldown_sec = 900.0
        self._tp_rehang_max_times = 4
        self._tp_rehang_net_headroom = 0.8
        self._tp_rehang_state_ttl_sec = ttl
        self._tp_rehang_state = {}
        self._max_net_notional_usdt = net_hard
        self._max_net_notional_soft_usdt = 0.0
        self._ct_val = ct_val
        self._last_tp_place_failure = {}
        self.cancelled = []
        self.placed = []
        self.save_calls = 0
        self.last_saved = None

    # --- host primitives ------------------------------------------------
    def _tp_lid_side(self, lid):
        return self._layer_side_map.get(str(lid), "")

    def _round_tp_price_away(self, side, price):
        return round(price, 5)

    def _minimum_profitable_tp_price(self, side, qty, entry):
        return entry * 1.002 if side == "long" else entry * 0.998

    def _cancel_tp_order(self, lid):
        self.cancelled.append(lid)

    def _place_tp_order(self, lid, side, qty, tp_price, coverage_safety=False, exclude_qty=0.0):
        self.placed.append((lid, side, qty, tp_price))
        return True

    def _save_pnl_data(self):
        self.save_calls += 1
        self.last_saved = self._tp_rehang_state_payload()

    def add_layer(self, lid, side, tp, qty):
        self._layer_tps[lid] = tp
        self._layer_side_map[lid] = side
        self._layer_qty_map[lid] = qty


class _StateHost(StateMixin):
    """Bare host for the pure (de)serialisation helpers."""

    def __init__(self, ttl=86400.0):
        self._tp_rehang_state_ttl_sec = ttl
        self._tp_rehang_state = {}


def run(fake, price):
    TpMixin._rehang_far_tps(fake, price)


def _roundtrip(raw):
    """payload -> JSON -> sanitize, exactly what a restart does."""
    host = _StateHost()
    blob = json.dumps(raw)
    return StateMixin._sanitize_tp_rehang_state(host, json.loads(blob))


# --------------------------------------------------------------- payload
def test_payload_is_json_safe_and_normalised():
    f = _Fake()
    f._tp_rehang_state = {"7": {"n": 2, "ts": 1700000000.4, "last": 1.234567891, "skip_ts": 0}}
    payload = StateMixin._tp_rehang_state_payload(f)
    assert payload == {"7": {"n": 2, "ts": 1700000000.4, "last": 1.23456789, "skip_ts": 0.0}}
    json.dumps(payload)  # must not raise


def test_payload_tolerates_garbage():
    f = _Fake()
    f._tp_rehang_state = {"a": "not-a-dict", "b": {"n": "x", "ts": None}}
    payload = StateMixin._tp_rehang_state_payload(f)
    assert "a" not in payload
    assert payload["b"]["n"] == 0


# -------------------------------------------------------------- sanitize
def test_sanitize_drops_junk_entries():
    host = _StateHost()
    out = StateMixin._sanitize_tp_rehang_state(host, {"1": 5, "2": {"n": -3, "ts": 9e18}})
    assert "1" not in out
    assert out["2"]["n"] == 0            # negative counters clamp to zero
    assert StateMixin._sanitize_tp_rehang_state(host, None) == {}


def test_sanitize_releases_expired_records():
    # ttl 1h, record stamped two hours ago -> budget must be released
    host = _StateHost(ttl=3600.0)
    old = {"1": {"n": 4, "ts": 1000.0}, "2": {"n": 4, "ts": 9e18}}
    out = StateMixin._sanitize_tp_rehang_state(host, old)
    assert "1" not in out
    assert out["2"]["n"] == 4


def test_sanitize_keeps_fresh_records():
    host = _StateHost(ttl=3600.0)
    fresh = {"3": {"n": 2, "ts": 9e18, "last": 1.5, "skip_ts": 9e18}}
    out = StateMixin._sanitize_tp_rehang_state(host, fresh)
    assert out["3"]["n"] == 2 and out["3"]["last"] == 1.5


def test_roundtrip_preserves_counters():
    f = _Fake()
    f._tp_rehang_state = {"4": {"n": 3, "ts": 9e18, "last": 1.31, "skip_ts": 0}}
    restored = _roundtrip(StateMixin._tp_rehang_state_payload(f))
    assert restored["4"] == {"n": 3, "ts": 9e18, "last": 1.31, "skip_ts": 0.0}


# --------------------------------------------------- behaviour after load
def test_restored_budget_blocks_further_rehang():
    # exactly what a restart looks like: counters come back from disk at max
    f = _Fake(long_qty=100.0, long_entry=0.9)
    f.add_layer("L1", "long", 1.30, 50)
    f._tp_rehang_state = {"L1": {"n": 4, "ts": 9e18, "last": 1.30}}
    run(f, 1.0)
    assert f.placed == []
    assert f.save_calls == 0


def test_successful_rehang_persists_new_counter():
    f = _Fake(long_qty=100.0, long_entry=0.9)
    f.add_layer("L1", "long", 1.30, 50)
    run(f, 1.0)
    assert len(f.placed) == 1
    assert f.save_calls == 1
    assert f.last_saved["L1"]["n"] == 1
    assert f.last_saved["L1"]["last"] > 0


def test_expired_record_is_released_and_rehang_allowed():
    f = _Fake(long_qty=100.0, long_entry=0.9, ttl=60.0)
    f.add_layer("L1", "long", 1.30, 50)
    f._tp_rehang_state = {"L1": {"n": 4, "ts": 1000.0}}   # ancient -> expired
    run(f, 1.0)
    assert len(f.placed) == 1
    assert f.save_calls == 1
    assert f.last_saved["L1"]["n"] == 1


def test_dropping_tp_state_clears_budget():
    f = _Fake()
    f.add_layer("L1", "long", 1.30, 50)
    f._tp_rehang_state = {"L1": {"n": 4, "ts": 9e18, "last": 1.30}}
    TpMixin._drop_tp_state_for_lids(f, ["L1"])
    assert "L1" not in f._tp_rehang_state
    # a reused lid therefore starts with a fresh budget
    f.add_layer("L1", "long", 1.30, 50)
    run(f, 1.0)
    assert len(f.placed) == 1
