from scripts.portfolio_risk_calculator import (
    RiskBands,
    build_deleveraging_candidates,
    calculate_portfolio,
    classify_account_state,
    required_margin_release,
)


def test_account_state_enters_deleveraging_before_hard_stop():
    bands = RiskBands()
    assert classify_account_state(0.71, 19.0, bands) == "REPAIR"
    assert classify_account_state(0.73, 19.0, bands) == "DELEVERAGE"
    assert classify_account_state(0.76, 19.0, bands) == "HARD_STOP"


def test_low_mgn_ratio_forces_hard_stop_even_below_margin_limit():
    assert classify_account_state(0.50, 9.9) == "HARD_STOP"


def test_required_release_and_candidates_only_reduce_dominant_side():
    positions = [
        {"symbol": "DRAM-USDT-SWAP", "pos_side": "short", "quantity": 31.7, "notional": 1773.85, "imr": 591.63},
        {"symbol": "DRAM-USDT-SWAP", "pos_side": "long", "quantity": 12.8, "notional": 716.26, "imr": 238.89},
        {"symbol": "CRV-USDT-SWAP", "pos_side": "long", "quantity": 2325, "notional": 586.95, "imr": 117.46},
        {"symbol": "CRV-USDT-SWAP", "pos_side": "short", "quantity": 2775, "notional": 700.56, "imr": 140.19},
    ]
    result = build_deleveraging_candidates(positions, 1675.94, 0.7308, 0.70)
    assert required_margin_release(1675.94, 0.7308, 0.70) > 0
    assert {x["symbol"] + ":" + x["pos_side"] for x in result} == {
        "DRAM-USDT-SWAP:short", "CRV-USDT-SWAP:short"
    }
    assert all(x["reduce_only_side"] == "buy" for x in result)
    assert all(x["recommended_qty_if_first"] <= x["live_qty"] for x in result)


def test_calculator_is_explicitly_read_only():
    result = calculate_portfolio({"equity": 1000, "margin_load": 0.72, "mgn_ratio": 18, "positions": []})
    assert result["read_only"] is True
    assert result["state"] == "REPAIR"


def test_symbol_permission_separates_account_state_from_symbol_skew():
    from scripts.portfolio_risk_calculator import symbol_permission
    p = symbol_permission("REPAIR", 100.0, 300.0, True, True)
    assert p["mode"] == "REPAIR_ONLY"
    assert p["allow_normal_grid"] is False
    assert p["allow_repair"] is True
    assert p["repair_side"] == "long"


def test_symbol_permission_hard_stop_never_allows_opening_or_repair():
    from scripts.portfolio_risk_calculator import symbol_permission
    p = symbol_permission("HARD_STOP", 100.0, 300.0, True, True)
    assert p["mode"] == "DELEVERAGE_ONLY"
    assert p["allow_normal_grid"] is False
    assert p["allow_repair"] is False


def test_symbol_permission_tp_gap_blocks_only_symbol_opening():
    from scripts.portfolio_risk_calculator import symbol_permission
    assert symbol_permission("NORMAL", 100.0, 300.0, False, True)["reason"] == "tp_coverage_incomplete"
