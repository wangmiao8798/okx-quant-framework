from scripts.huma_dual_strategy import build_plan


def test_huma_plan_uses_contract_value_and_one_contract_per_level():
    plan = build_plan(
        price=0.02120,
        ct_val=100.0,
        tick_size=0.00001,
        levels=3,
        grid_step=0.008,
        tp_ratio=0.012,
    )
    assert plan['symbol'] == 'HUMA-USDT-SWAP'
    assert plan['contract_notional_usdt'] == 2.12
    assert [item['contracts'] for item in plan['long_entries']] == [1, 1, 1]
    assert [item['contracts'] for item in plan['short_entries']] == [1, 1, 1]
    assert plan['max_side_notional_usdt'] < 7.0


def test_huma_plan_places_entries_on_both_sides_and_profit_tps():
    plan = build_plan(0.02120, 100.0, 0.00001, levels=3, grid_step=0.008, tp_ratio=0.012)
    assert all(item['entry'] < 0.02120 for item in plan['long_entries'])
    assert all(item['tp'] > item['entry'] for item in plan['long_entries'])
    assert all(item['entry'] > 0.02120 for item in plan['short_entries'])
    assert all(item['tp'] < item['entry'] for item in plan['short_entries'])


def test_huma_plan_never_contains_loss_rebalance():
    plan = build_plan(0.02120, 100.0, 0.00001)
    assert plan['loss_rebalance_enabled'] is False
    assert plan['live_enabled'] is False
