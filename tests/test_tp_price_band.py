# -*- coding: utf-8 -*-
"""Tests for the TP price-band clamp (OKX 51006).

A take-profit derived from a stale entry price can land outside the
exchange's allowed price band, where OKX rejects it outright and the
strategy retries forever with no coverage. The clamp must:

  * lift a *profitable* long TP to just beyond market;
  * lower a *profitable* short TP to just below market;
  * never move a TP into the loss side (that must stay rejected);
  * stay inert when disabled or when no market reference exists.
"""
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
    def __init__(self, side="long", qty=1670.0, entry=0.3458, mark=0.3635,
                 band_enabled=True, gap=0.003):
        self.log = _Log()
        self._client = object()
        self._symbol = "CRV-USDT-SWAP"
        self._ct_val = 1.0
        self._qty_step = 1.0
        self._min_qty = 1.0
        self._tick_size = 0.0001
        self._long = _Side(qty if side == "long" else 0.0, entry if side == "long" else 0.0)
        self._short = _Side(qty if side == "short" else 0.0, entry if side == "short" else 0.0)
        self._layer_tps = {}
        self._layer_qty_map = {}
        self._layer_side_map = {}
        self._layer_tp_orders = {}
        self._tp_rehang_state = {}
        self._tp_no_position_until = {}
        self._tp_price_band_enabled = band_enabled
        self._tp_price_band_gap_ratio = gap
        self._last_price = mark
        self._qty = qty
        self._entry = entry
        self.submitted = []

    # --- host hooks used by _place_tp_order ---
    def _exchange_position_snapshot(self, side):
        return self._qty, self._entry

    def _position_qty_tolerance(self):
        return 0.0

    def _minimum_profitable_tp_price(self, side, qty, entry):
        # Mirrors production semantics: a floor above entry for longs and a
        # ceiling below entry for shorts. Returning 0 would zero the sell price.
        return entry * (1.000001 if side == "long" else 0.999999)

    def _round_tp_price_away(self, side, price):
        return round(price, 6)

    def _submit_order(self, symbol, side, sz, **kwargs):
        self.submitted.append({"side": side, "sz": float(sz), "px": float(kwargs.get("px"))})
        return {"ordId": "TEST-%d" % len(self.submitted)}


def place(f, pside, qty, tp):
    ok = f._place_tp_order("0", pside, qty, tp)
    return ok, (f.submitted[-1]["px"] if f.submitted else None)


def test_profitable_long_tp_lifted_past_market():
    f = _Fake(side="long", entry=0.3458, mark=0.3635)
    ok, px = place(f, "long", 1670, 0.3486)
    assert ok
    assert px > 0.3635          # pushed past market
    assert px > 0.3458          # still profitable vs entry


def test_losing_long_tp_not_moved():
    """entry above market: the clamp must not invent a loss-making exit."""
    f = _Fake(side="long", entry=0.40, mark=0.3635)
    ok, px = place(f, "long", 100, 0.4032)
    assert ok
    assert px == pytest.approx(0.4032)


def test_profitable_short_tp_lowered_below_market():
    f = _Fake(side="short", entry=0.40, mark=0.3635)
    ok, px = place(f, "short", 100, 0.396)
    assert ok
    assert px <= 0.3635 * 0.997 + 1e-9
    assert px < 0.40


def test_losing_short_tp_not_moved():
    f = _Fake(side="short", entry=0.33, mark=0.3635)
    ok, px = place(f, "short", 100, 0.327)
    assert ok
    assert px == pytest.approx(0.327)


def test_losing_short_clamp_would_be_a_loss_stays_put():
    """short entry below the clamped price means realising a loss -> skip clamp."""
    f = _Fake(side="short", entry=0.362, mark=0.3635, gap=0.003)
    ok, px = place(f, "short", 100, 0.359)
    assert px == pytest.approx(0.359)


def test_disabled_band_keeps_original_price():
    f = _Fake(side="long", entry=0.3458, mark=0.3635, band_enabled=False)
    ok, px = place(f, "long", 1670, 0.3486)
    assert ok
    assert px == pytest.approx(0.3486)


def test_zero_market_reference_keeps_original_price():
    f = _Fake(side="long", entry=0.3458, mark=0.0)
    ok, px = place(f, "long", 1670, 0.3486)
    assert ok
    assert px == pytest.approx(0.3486)


def test_band_gap_respected_exactly():
    f = _Fake(side="long", entry=0.30, mark=0.3635, gap=0.01)
    ok, px = place(f, "long", 100, 0.3024)
    assert ok
    assert px == pytest.approx(round(0.3635 * 1.01, 6), abs=1e-6)
