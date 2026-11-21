from types import SimpleNamespace
from pathlib import Path

from strategies.futures_hedge import FuturesHedgeStrategy, HedgeInventory, Pos
from risk.position_limits import opening_limits_block_reason


def _strategy(margin_load=0.516):
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._inventory = HedgeInventory()
    strategy._inventory.long = Pos("long", 100.0, 100.0, 0.0, 0)
    strategy._inventory.short = Pos("short", 100.0, 66.0, 0.0, 0)
    strategy._min_qty = 1.0
    strategy._active_inventory_repair_enabled = True
    strategy._inventory_repair_max_margin_ratio = 0.54
    strategy._inventory_repair_missing_side_margin_ratio = 0.54
    strategy._account_margin_hard_limit = 0.55
    strategy._hard_margin_ratio = 0.55
    strategy._account_margin_data_block_reason = lambda: ""
    strategy._mgn_ratio_opening_block_reason = lambda: ""
    strategy._account_margin_load_ratio = lambda: margin_load
    return strategy


def test_active_repair_is_not_rejected_by_grid_bias_threshold():
    strategy = _strategy()
    # (100 - 66) / 166 ~= 20.5%, below a 35% grid bias threshold.
    strategy._grid_bias_ratio = 0.35
    assert strategy._inventory_repair_margin_blocked() is False


def test_repair_still_respects_the_hard_margin_line():
    strategy = _strategy(margin_load=0.55)
    assert strategy._inventory_repair_margin_blocked() is True


def test_crv_hard_margin_repair_uses_the_same_policy_decision_and_step_cap():
    strategy = _strategy(margin_load=0.6177)
    strategy._symbol = "CRV-USDT-SWAP"
    strategy._ct_val = 1.0
    strategy._qty_step = 1.0
    strategy._cached_equity = 1838.5
    strategy._cached_frozen = 1135.64895
    strategy._cur_lev = 5.0
    strategy._margin_buffer_ratio = 0.10
    strategy._account_margin_soft_limit = 0.52
    strategy._account_margin_hard_limit = 0.60
    strategy._account_margin_resume_limit = 0.599
    strategy._account_margin_scope = "crv_strategy_test"
    strategy._max_margin_ratio = 0.32
    strategy._max_net_notional_usdt = 150.0
    strategy._account_risk_max_age_sec = 30.0
    strategy._account_budget_allocator = None
    strategy._last_price = 0.2684
    strategy._inventory_repair_hard_override_enabled = True
    strategy._inventory_repair_hard_override_symbol = "CRV-USDT-SWAP"
    strategy._inventory_repair_hard_override_margin_ratio = 0.63
    strategy._inventory_repair_hard_override_mgn_ratio_min = 12.0
    strategy._inventory_repair_hard_override_max_step_notional_usdt = 8.0
    strategy._account_risk_snapshot = {"usdt": {"mgnRatio": "20"}}
    strategy._risk_context_snapshot = lambda: {
        "symbol": "CRV-USDT-SWAP",
        "positions": {
            "long_qty": 0.0,
            "short_qty": 2775.0,
            "max_net_exposure": 0.0,
            "max_net_notional_usdt": 150.0,
        },
        "account": {
            "margin_load_ratio": 0.6177,
            "equity_usdt": 1838.5,
            "frozen_margin_usdt": 1135.64895,
            "account_margin_hard_limit": 0.60,
            "account_margin_soft_limit": 0.52,
            "account_margin_resume_limit": 0.599,
            "account_budget_enabled": False,
            "mark_price": 0.2684,
            "contract_value": 1.0,
            "leverage": 5.0,
            "margin_buffer_ratio": 0.10,
            "inventory_repair_hard_override_enabled": True,
            "inventory_repair_hard_override_symbol": "CRV-USDT-SWAP",
            "inventory_repair_hard_override_margin_ratio": 0.63,
            "inventory_repair_hard_override_mgn_ratio_min": 12.0,
            "inventory_repair_hard_override_max_step_notional_usdt": 8.0,
        },
        "account_risk": {"usdt": {"mgnRatio": "20"}},
    }

    assert strategy._inventory_repair_step_qty_cap("long", 0.2684, 100.0) == 29.0
    assert strategy._inventory_repair_margin_blocked("long", 0.2684, 27.0) is False
    assert strategy._inventory_repair_hard_override_last["allowed"] is True


def test_balanced_hedge_uses_account_soft_limit_for_inventory_pause():
    strategy = _strategy(margin_load=0.39)
    strategy._long = Pos("long", 100.0, 100.0, 0.0, 0)
    strategy._short = Pos("short", 100.0, 100.0, 0.0, 0)
    strategy._inventory_margin_stop_ratio = 0.35
    strategy._inventory_margin_resume_ratio = 0.30
    strategy._inventory_margin_hedge_credit_ratio = 0.20
    strategy._max_margin_ratio = 0.32
    strategy._account_margin_soft_limit = 0.45

    stop_ratio, resume_ratio, hedge_ratio = strategy._inventory_margin_thresholds()

    assert hedge_ratio == 0.0
    assert stop_ratio == 0.45
    assert resume_ratio == 0.40


def test_directional_inventory_keeps_base_inventory_pause_line():
    strategy = _strategy(margin_load=0.39)
    strategy._long = Pos("long", 100.0, 100.0, 0.0, 0)
    strategy._short = Pos("short", 100.0, 10.0, 0.0, 0)
    strategy._inventory_margin_stop_ratio = 0.35
    strategy._inventory_margin_resume_ratio = 0.30
    strategy._inventory_margin_hedge_credit_ratio = 0.20
    strategy._max_margin_ratio = 0.32
    strategy._account_margin_soft_limit = 0.45

    stop_ratio, resume_ratio, hedge_ratio = strategy._inventory_margin_thresholds()

    assert hedge_ratio > 0.8
    assert stop_ratio < 0.40
    assert resume_ratio < stop_ratio


def test_normal_weak_side_order_cannot_use_repair_side_cap_credit():
    reason = opening_limits_block_reason(
        "short",
        long_before=100.0,
        short_before=100.0,
        quantity=10.0,
        contract_value=1.0,
        price=1.0,
        side_cap=105.0,
        net_cap=1000.0,
        inventory_repair_side_cap_multiplier=1.10,
        inventory_repair=True,
    )
    assert reason == "max_side_notional"


def test_explicit_net_reducing_repair_can_use_bounded_side_cap_credit():
    reason = opening_limits_block_reason(
        "short",
        long_before=100.0,
        short_before=90.0,
        quantity=10.0,
        contract_value=1.0,
        price=1.0,
        side_cap=95.0,
        net_cap=1000.0,
        inventory_repair_side_cap_multiplier=1.10,
        inventory_repair=True,
    )
    assert reason == ""


def test_repair_marker_cannot_authorize_net_increasing_order():
    reason = opening_limits_block_reason(
        "long",
        long_before=100.0,
        short_before=90.0,
        quantity=10.0,
        contract_value=1.0,
        price=1.0,
        side_cap=1000.0,
        net_cap=1000.0,
        inventory_repair=True,
    )
    assert reason == ""
    reason = opening_limits_block_reason(
        "long",
        long_before=100.0,
        short_before=90.0,
        quantity=11.0,
        contract_value=1.0,
        price=1.0,
        side_cap=1000.0,
        net_cap=10.0,
        inventory_repair=True,
    )
    assert reason == "max_net_notional"


def test_dram_uses_one_margin_band_for_grid_and_inventory_repair():
    import pytest
    import yaml
    root = Path(__file__).resolve().parents[1]
    dram_path = root / "config" / "config_dram.yaml"
    if not dram_path.exists():
        pytest.skip("config_dram.yaml 已停用（DRAM 策略退役）")
    futures = yaml.safe_load(dram_path.read_text())["futures"]
    assert futures["account_margin_soft_limit"] == futures["margin_first_normal_margin_ratio"] == 0.20
    assert futures["inventory_repair_max_margin_ratio"] == 0.15
    assert futures["margin_first_max_margin_ratio"] == 0.55

    strategy = _strategy(margin_load=0.53)
    strategy._symbol = "DRAM-USDT-SWAP"
    strategy._margin_first_capacity_enabled = True
    strategy._margin_first_normal_margin_ratio = 0.52
    strategy._margin_first_max_margin_ratio = 0.55
    assert strategy._inventory_repair_margin_blocked() is False


def test_dram_unified_repair_keeps_existing_maker_grids():
    strategy = _strategy(margin_load=0.53)
    strategy._symbol = "DRAM-USDT-SWAP"
    strategy._hedged_grid_allow_dominant_refill = True
    strategy._margin_first_capacity_enabled = True
    strategy._margin_first_max_margin_ratio = 0.55
    strategy._account_margin_data_block_reason = lambda: ""
    strategy._mgn_ratio_opening_block_reason = lambda: ""
    assert strategy._preserve_opening_grids_during_inventory_repair() is True


def test_throttled_account_blocks_only_dominant_grid_side():
    strategy = bare_strategy()
    strategy._account_margin_soft_limit = 0.52
    strategy._account_margin_hard_limit = 0.60
    strategy._account_margin_load_ratio = lambda: 0.55
    strategy._long = Pos("long", qty=30.0)
    strategy._short = Pos("short", qty=10.0)
    strategy._min_qty = 1.0
    assert strategy._portfolio_throttled_dominant_side_blocked("long") is True
    assert strategy._portfolio_throttled_dominant_side_blocked("short") is False


def test_hard_account_margin_blocks_both_normal_grid_sides():
    strategy = bare_strategy()
    strategy._account_margin_soft_limit = 0.52
    strategy._account_margin_hard_limit = 0.60
    strategy._account_margin_load_ratio = lambda: 0.73
    strategy._long = Pos("long", qty=30.0)
    strategy._short = Pos("short", qty=10.0)
    strategy._min_qty = 1.0
    assert strategy._portfolio_throttled_dominant_side_blocked("long") is False
    assert strategy._portfolio_throttled_dominant_side_blocked("short") is False


def test_throttled_account_blocks_only_dominant_grid_side():
    strategy = _strategy(margin_load=0.55)
    strategy._account_margin_soft_limit = 0.52
    strategy._account_margin_hard_limit = 0.60
    strategy._long = Pos("long", qty=30.0)
    strategy._short = Pos("short", qty=10.0)
    strategy._min_qty = 1.0
    assert strategy._portfolio_throttled_dominant_side_blocked("long") is True
    assert strategy._portfolio_throttled_dominant_side_blocked("short") is False


def test_hard_account_margin_blocks_both_normal_grid_sides():
    strategy = _strategy(margin_load=0.73)
    strategy._account_margin_soft_limit = 0.52
    strategy._account_margin_hard_limit = 0.60
    strategy._long = Pos("long", qty=30.0)
    strategy._short = Pos("short", qty=10.0)
    strategy._min_qty = 1.0
    assert strategy._portfolio_throttled_dominant_side_blocked("long") is False
    assert strategy._portfolio_throttled_dominant_side_blocked("short") is False
