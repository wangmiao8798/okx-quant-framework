from strategies.futures_hedge import FuturesHedgeStrategy


def make_strategy(center=64.0, last=55.7):
    strategy = object.__new__(FuturesHedgeStrategy)
    strategy._grid_center_price = center
    strategy._last_price = last
    strategy._long_grid_levels = [53.0, 54.0, 55.0]
    strategy._short_grid_levels = [63.0, 64.0, 65.0]
    strategy._in_startup_recovery = lambda: False
    strategy._in_post_recovery_refill_delay = lambda: False
    strategy._effective_drift_threshold = lambda: 0.015
    strategy._add_step = 0.005
    strategy._max_adds = 5
    strategy._target_grid_layers = lambda: 3
    strategy._order_refresh_tolerance_pct = 0.0025
    strategy.log = type('Log', (), {'info': lambda *args, **kwargs: None})()
    strategy._rebuild_grid = lambda: {'type': 'grid_rebuild'}
    return strategy


def test_drift_rebuilds_even_when_both_grid_sides_exist():
    strategy = make_strategy()
    assert strategy._check_drift() == {'type': 'grid_rebuild'}


def test_stale_grid_levels_rebuild_even_when_center_is_current():
    strategy = make_strategy(center=55.7, last=55.7)
    strategy._long_grid_levels = [53.63, 54.33, 54.72]
    strategy._short_grid_levels = [62.98, 63.41, 64.20]
    strategy._add_step = 0.005
    strategy._target_grid_layers = lambda: 3
    assert strategy._check_drift() == {'type': 'grid_rebuild'}


def test_drift_does_not_rebuild_inside_threshold():
    strategy = make_strategy(center=56.0, last=55.7)
    strategy._long_grid_levels = [55.42, 55.14, 54.86]
    strategy._short_grid_levels = [55.98, 56.26, 56.54]
    assert strategy._check_drift() is None


def test_active_grid_side_suppresses_duplicate_market_add():
    strategy = make_strategy(center=56.0, last=55.7)
    assert strategy._active_grid_side("long") is True
    strategy._long_grid_levels = []
    assert strategy._active_grid_side("long") is False
