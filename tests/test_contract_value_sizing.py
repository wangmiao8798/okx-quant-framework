from types import SimpleNamespace

from strategies.futures_hedge import FuturesHedgeStrategy
from strategies.hedge_calculations import (
    contract_qty_for_margin,
    effective_tp_ratio,
    position_unrealized_pnl,
)


def test_contract_qty_respects_non_unit_contract_value():
    qty = contract_qty_for_margin(
        margin_usdt=6.0,
        price=4289.2,
        leverage=3.0,
        contract_value=0.001,
        qty_step=1.0,
        min_qty=1.0,
    )

    assert qty == 4.0


def test_unit_contract_sizing_is_unchanged():
    qty = contract_qty_for_margin(
        margin_usdt=1.5,
        price=0.34,
        leverage=5.0,
        contract_value=1.0,
        qty_step=1.0,
        min_qty=1.0,
    )

    assert qty == 22.0


def test_position_pnl_respects_non_unit_contract_value():
    pnl = position_unrealized_pnl(
        "long",
        last_price=4290.2,
        long_qty=1.0,
        long_entry=4289.2,
        short_qty=0.0,
        short_entry=0.0,
        contract_value=0.001,
    )

    assert pnl == 0.001


def test_xau_tp_remains_profitable_with_taker_open_assumption():
    qty = 3.0
    entry = 4287.0
    exit_price = entry * (1.0 + effective_tp_ratio(
        target_ratio=0.004,
        min_edge_ratio=0.0025,
        order_mode="maker",
        taker_fee=0.0005,
        maker_fee=0.0002,
    ))
    gross = qty * 0.001 * (exit_price - entry)
    open_fee = qty * 0.001 * entry * 0.0005
    close_fee = qty * 0.001 * exit_price * 0.0002
    funding_buffer = qty * 0.001 * entry * 0.0015

    assert gross - open_fee - close_fee - funding_buffer >= 0.01


def test_eth_tp_remains_profitable_with_taker_open_assumption():
    qty = 0.04
    entry = 2692.5
    exit_price = entry * (1.0 + effective_tp_ratio(
        target_ratio=0.0035,
        min_edge_ratio=0.0025,
        order_mode="maker",
        taker_fee=0.0005,
        maker_fee=0.0002,
    ))
    gross = qty * 0.1 * (exit_price - entry)
    open_fee = qty * 0.1 * entry * 0.0005
    close_fee = qty * 0.1 * exit_price * 0.0002
    funding_buffer = qty * 0.1 * entry * 0.001

    assert gross - open_fee - close_fee - funding_buffer >= 0.01


def test_flat_exchange_state_keeps_initial_hedge_pending():
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._fetch_position_rows = lambda: []
    strategy._position_qty_tolerance = lambda: 0.0
    strategy._last_price = 100.0
    strategy._init_done = False
    strategy.log = SimpleNamespace(
        info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)

    result = strategy._sync_exchange_state()

    assert result is False
    assert strategy._init_done is False
