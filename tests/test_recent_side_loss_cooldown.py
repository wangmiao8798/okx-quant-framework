# -*- coding: utf-8 -*-  # noqa: INP001
"""Recent-side-loss guard cooldown bounds.

The guard used to scale realised loss by 900x, so a 2 U loss froze the
side for a full 30 minutes while the edge floor was doubled. On a 1000 U
account that fired on ordinary grid noise and idled the strategy.

These tests pin the relaxed curve (300x, capped at 15 min, floored at 5 min)
and the behaviours around it.
"""
import time

import pytest

from strategies.hedge_kit.state import StateMixin


class _Host(StateMixin):
    def __init__(self):
        self._recent_side_loss_until = {"long": 0.0, "short": 0.0}


def _remaining(host, side):
    return host._recent_side_loss_until[side] - time.time()


@pytest.mark.parametrize(
    "loss,expected",
    [
        (0.1, 300.0),
        (1.0, 300.0),
        (2.0, 600.0),
        (20.0, 900.0),
    ],
)
def test_cooldown_scales_with_loss_within_bounds(loss, expected):
    host = _Host()
    host._mark_side_realized_pnl("long", -loss)
    remaining = _remaining(host, "long")
    assert remaining == pytest.approx(expected, abs=2.0)
    assert 300.0 - 1.0 <= remaining <= 900.0 + 1.0


def test_profit_never_arms_the_guard():
    host = _Host()
    host._mark_side_realized_pnl("long", 1.5)
    assert host._recent_side_loss_until["long"] == 0.0


def test_zero_pnl_never_arms_the_guard():
    host = _Host()
    host._mark_side_realized_pnl("short", 0.0)
    assert host._recent_side_loss_until["short"] == 0.0


def test_guard_is_per_side():
    host = _Host()
    host._mark_side_realized_pnl("long", -2.0)
    assert host._recent_side_loss_until["short"] == 0.0
    assert _remaining(host, "long") > 0.0


def test_unknown_side_is_ignored():
    host = _Host()
    host._mark_side_realized_pnl("both", -2.0)
    assert host._recent_side_loss_until == {"long": 0.0, "short": 0.0}


def test_longer_existing_cooldown_is_not_shortened():
    host = _Host()
    host._recent_side_loss_until["long"] = time.time() + 800.0
    host._mark_side_realized_pnl("long", -1.0)
    assert _remaining(host, "long") == pytest.approx(800.0, abs=2.0)
