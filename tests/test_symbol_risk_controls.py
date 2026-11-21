import time
from types import SimpleNamespace

from core.engine import MainEngine
from api.okx_client import OKXClient
from inventory.hedge_inventory import HedgeInventory
from risk.black_swan import BlackSwanProtector
from risk.liquidation_guard import LiquidationGuard
from strategies.futures_hedge import FuturesHedgeStrategy


def risk_config(**overrides):
    liquidation = {
        "enabled": True,
        "safety_distance_rate": 0.15,
        "emergency_distance_rate": 0.07,
        "confirmation_checks": 2,
        "pause_opening_seconds": 120,
        "action_cooldown_seconds": 60,
        "emergency_reduce_ratio": 0.25,
    }
    liquidation.update(overrides)
    return {"risk": {"liquidation": liquidation}}


def test_liquidation_guard_uses_validated_liquidation_distance():
    guard = LiquidationGuard(risk_config())

    safe = guard.evaluate({
        "mark_px": 100,
        "long_sz": 2,
        "long_liq": 80,
        "short_sz": 2,
        "short_liq": 125,
    })
    warning = guard.evaluate({
        "mark_px": 100,
        "long_sz": 2,
        "long_liq": 90,
        "short_sz": 2,
        "short_liq": 125,
    })

    assert safe["zone"] == "safe"
    assert safe["pause_opening"] is False
    assert warning["zone"] == "warning"
    assert warning["pause_opening"] is True
    assert warning["affected_sides"] == ["long"]


def test_position_risk_preserves_exchange_mark_price_and_liquidation_prices():
    client = OKXClient.__new__(OKXClient)
    client.get_positions = lambda _symbol: [
        {"posSide": "long", "pos": "2", "liqPx": "80", "markPx": "100", "notionalUsd": "200"},
        {"posSide": "short", "pos": "1", "liqPx": "125", "markPx": "100", "notionalUsd": "100"},
    ]

    risk = client.get_position_risk("TEST-USDT-SWAP")

    assert risk["mark_px"] == 100.0
    assert risk["long_liq"] == 80.0
    assert risk["short_liq"] == 125.0


def test_invalid_or_missing_liquidation_prices_never_trigger():
    guard = LiquidationGuard(risk_config())

    status = guard.evaluate({
        "mark_px": 100,
        "long_sz": 2,
        "long_liq": 110,
        "short_sz": 2,
        "short_liq": 90,
    })

    assert status["zone"] == "unavailable"
    assert status["pause_opening"] is False
    assert status["emergency_reduce"] is False


def test_emergency_liquidation_reduction_requires_confirmation_and_cooldown():
    guard = LiquidationGuard(risk_config())
    risk = {"mark_px": 100, "long_sz": 4, "long_liq": 95}

    first = guard.evaluate(risk)
    second = guard.evaluate(risk)
    third = guard.evaluate(risk)

    assert first["pause_opening"] is True
    assert first["emergency_reduce"] is False
    assert second["emergency_reduce"] is True
    assert third["emergency_reduce"] is False


def test_ordinary_price_change_does_not_freeze_black_swan_guard():
    guard = BlackSwanProtector({
        "risk": {"black_swan": {"enabled": True, "price_jump_threshold": 0.10, "consecutive_drop": 2}}
    })

    assert guard.check(101, 100) is False
    assert guard.is_frozen is False


def test_strategy_opening_pause_cancels_open_orders_once_and_auto_recovers():
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._risk_opening_pause_until = 0.0
    strategy._risk_opening_pause_reason = ""
    strategy._reduce_only_mode = False
    strategy._instrument_info_ready = True
    strategy._ct_val = 1.0
    cancelled = []
    strategy._cancel_opening_orders = lambda: cancelled.append(True)
    strategy._set_add_block = lambda *args, **kwargs: None

    strategy.pause_opening("abnormal_market", 60)
    strategy.pause_opening("abnormal_market", 60)

    assert strategy._opening_order_block_reason("long", 1, 100) == "abnormal_market"
    assert cancelled == [True]
    strategy._risk_opening_pause_until = time.time() - 1
    assert strategy.opening_pause_status() == {"active": False, "reason": "", "remaining_sec": 0.0}


def test_reduce_only_submission_remains_available_during_opening_pause():
    calls = []
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._symbol = "TEST-USDT-SWAP"
    strategy._client = object()
    strategy._risk_opening_pause_until = time.time() + 60
    strategy._risk_opening_pause_reason = "abnormal_market"
    strategy._execution_gateway = SimpleNamespace(
        place_order=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ordId": "reduce-1"}
    )

    result = strategy._submit_order(
        strategy._symbol,
        "sell",
        1,
        pos_side="long",
        reduce_only=True,
    )

    assert result == {"ordId": "reduce-1"}
    assert calls[0][1]["reduce_only"] is True


class LogStub:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class NotifierStub:
    def __init__(self):
        self.calls = []

    def alert(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_symbol_abnormal_market_pause_never_sets_global_halted():
    pauses = []
    strategy = SimpleNamespace(
        _symbol="TEST-USDT-SWAP",
        _cur_lev=2,
        pause_opening=lambda reason, seconds, cancel_existing=True: pauses.append(
            (reason, seconds, cancel_existing)
        ),
    )
    black_swan = SimpleNamespace(
        is_frozen=True,
        remaining=45.0,
        check=lambda *_args: False,
        check_thaw=lambda: False,
    )
    liquidation = SimpleNamespace(evaluate=lambda _risk: {}, pause_seconds=120, reduce_ratio=0.25)
    engine = MainEngine.__new__(MainEngine)
    engine.config = {}
    engine.client = SimpleNamespace(get_position_risk=lambda _symbol: {})
    engine.execution_gateway = None
    engine.risk_gate = SimpleNamespace(
        set_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not halt globally"))
    )
    engine.log = LogStub()
    engine.notifier = NotifierStub()
    engine._symbol_risk_manager = lambda *_args: SimpleNamespace(
        black_swan=black_swan,
        liquidation=liquidation,
    )

    engine._check_symbol_risk("futures_test", strategy, 100, 99)

    assert pauses == [("abnormal_market", 45.0, True)]
    assert engine.notifier.calls


def test_liquidation_emergency_uses_reduce_only_order():
    calls = []
    gateway = SimpleNamespace(place_order=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ordId": "1"})
    strategy = SimpleNamespace(_symbol="TEST-USDT-SWAP", _qty_step=0.1, _min_qty=0.1)
    engine = MainEngine.__new__(MainEngine)
    engine.execution_gateway = gateway

    results = engine._reduce_liquidation_risk(
        strategy,
        {"long_sz": 4.0, "short_sz": 0.0},
        ["long"],
        0.25,
    )

    assert results == [{"ordId": "1"}]
    assert calls[0][0] == ("TEST-USDT-SWAP", "sell", "1.0")
    assert calls[0][1]["reduce_only"] is True


def test_black_swan_only_pauses_openings_and_does_not_trigger_global_halt():
    pauses = []
    strategy = SimpleNamespace(
        _symbol="TEST-USDT-SWAP",
        _cur_lev=2,
        pause_opening=lambda reason, seconds, cancel_existing=True: pauses.append((reason, seconds)),
    )
    engine = MainEngine.__new__(MainEngine)
    engine.config = {}
    engine.client = SimpleNamespace(get_position_risk=lambda _symbol: {
        "mark_px": 100,
        "long_sz": 1,
        "long_liq": 80,
        "short_sz": 1,
        "short_liq": 125,
    })
    engine.execution_gateway = None
    engine.risk_gate = SimpleNamespace(
        set_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not set HALTED"))
    )
    engine.log = LogStub()
    notifications = []
    engine.notifier = SimpleNamespace(alert=lambda *args, **kwargs: notifications.append((args, kwargs)))
    engine._symbol_risk_manager = lambda *_args: SimpleNamespace(
        black_swan=SimpleNamespace(
            is_frozen=True,
            remaining=30.0,
            check=lambda *_a: False,
            check_thaw=lambda: False,
        ),
        liquidation=SimpleNamespace(evaluate=lambda _risk: {"available": False}),
    )

    engine._check_symbol_risk("futures_test", strategy, 100, 99)

    assert pauses == [("abnormal_market", 30.0)]
    assert notifications


def test_hedge_mode_missing_liquidation_data_does_not_pause_by_itself():
    pauses = []
    strategy = SimpleNamespace(
        _symbol="TEST-USDT-SWAP",
        _cur_lev=2,
        pause_opening=lambda reason, seconds, cancel_existing=True: pauses.append((reason, seconds)),
    )
    engine = MainEngine.__new__(MainEngine)
    engine.config = {}
    engine.client = SimpleNamespace(get_position_risk=lambda _symbol: {
        "positions_available": True,
        "long_sz": 2,
        "long_liq": 0,
        "mark_px": 100,
    })
    engine.execution_gateway = None
    engine.risk_gate = SimpleNamespace(set_state=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    engine.log = LogStub()
    engine.notifier = SimpleNamespace(alert=lambda *_args, **_kwargs: None)
    engine._symbol_risk_manager = lambda *_args: SimpleNamespace(
        black_swan=SimpleNamespace(is_frozen=False, remaining=0.0, check=lambda *_a: False, check_thaw=lambda: True),
        liquidation=SimpleNamespace(
            evaluate=lambda _risk: {"zone": "unavailable", "positions_present": True},
            pause_seconds=120,
            reduce_ratio=0.25,
        ),
    )

    engine._check_symbol_risk("futures_test", strategy, 100, 99)

    # In hedge mode OKX may omit side-specific liqPx. Account-level margin
    # health is checked by the strategy; this position-risk observation alone
    # must not pause otherwise valid hedged openings.
    assert pauses == []


def test_opening_pause_keeps_reduce_only_order_path_open():
    calls = []
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._symbol = "TEST-USDT-SWAP"
    strategy._client = object()
    strategy._risk_opening_pause_until = time.time() + 60
    strategy._risk_opening_pause_reason = "abnormal_market"
    strategy._execution_gateway = SimpleNamespace(
        place_order=lambda *args, **kwargs: calls.append((args, kwargs)) or {"ordId": "reduce-1"}
    )
    strategy._submit_order = strategy._submit_order.__get__(strategy, FuturesHedgeStrategy)

    assert strategy._opening_order_block_reason("long", 1, 100) == "abnormal_market"
    assert strategy._submit_order("TEST-USDT-SWAP", "sell", 1, pos_side="long", reduce_only=True) == {"ordId": "reduce-1"}
    assert calls[0][1]["pos_side"] == "long"


def test_existing_hedge_requires_fresh_account_margin_snapshot():
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._client = object()
    strategy._inventory = HedgeInventory()
    strategy._account_risk_max_age_sec = 15.0
    strategy._mgn_ratio_open_limit = 5.0
    strategy._long.qty = 10.0
    strategy._account_risk_snapshot = {
        "observed_at_ms": int((time.time() - 30) * 1000),
        "usdt": {"mgnRatio": "42"},
    }
    assert strategy._mgn_ratio_opening_block_reason().startswith("account_margin_data_stale")


def test_hedged_position_can_open_with_fresh_account_margin_even_without_liqpx():
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._client = object()
    strategy._inventory = HedgeInventory()
    strategy._account_risk_max_age_sec = 15.0
    strategy._mgn_ratio_open_limit = 5.0
    strategy._long.qty = 10.0
    strategy._short.qty = 10.0
    strategy._account_risk_snapshot = {
        "observed_at_ms": int(time.time() * 1000),
        "usdt": {"mgnRatio": "42"},
    }
    assert strategy._mgn_ratio_opening_block_reason() == ""
