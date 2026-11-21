import ast
import sys
import threading
import pytest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.futures_hedge import FuturesHedgeStrategy, Pos
from strategies.hedge_calculations import (
    atr_adaptive_step,
    hard_hedge_order_qty,
    inventory_net_size_multiplier,
)
from core.engine import MainEngine
from api.okx_client import OKXClient


class LogStub:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def bare_strategy():
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy.log = LogStub()
    strategy._min_qty = 1.0
    strategy._qty_step = 1.0
    strategy._tick_size = 0.1
    strategy._add_step = 0.005
    strategy._base_tp_ratio = 0.02
    strategy._first_tp = 0.02
    strategy._final_tp = 0.04
    strategy._trail = 0.015
    strategy._side_config = {}
    strategy._tp_no_position_until = {'long': 0.0, 'short': 0.0}
    strategy._min_expected_net_usdt = 0.05
    strategy._tp_funding_buffer_ratio = 0.001
    strategy._tp_compact_target_orders = 5
    strategy._base_pos_usdt = 50.0
    strategy._symbol = 'TEST-USDT-SWAP'
    strategy._ct_val = 1.0
    strategy._instrument_info_ready = True
    strategy._max_side_notional_usdt = 0.0
    strategy._max_net_notional_usdt = 0.0
    strategy._cb_pause_until = 0.0
    strategy._client = None
    strategy._next_layer_id = 0
    strategy._layer_tps = {}
    strategy._layer_qty_map = {}
    strategy._layer_side_map = {}
    strategy._layer_tp_orders = {}
    strategy._long = Pos('long')
    strategy._short = Pos('short')
    strategy._cur_lev = 2
    strategy._effective_tp_ratio = lambda ratio, _mode='maker': ratio
    return strategy


def test_inventory_net_size_multiplier_uses_soft_and_hard_limits():
    assert inventory_net_size_multiplier("short", "short", 39.0, 40.0, 80.0) == 1.0
    assert 0.0 < inventory_net_size_multiplier("short", "short", 60.0, 40.0, 80.0) < 1.0
    assert inventory_net_size_multiplier("short", "short", 80.0, 40.0, 80.0) == 0.0
    assert inventory_net_size_multiplier("long", "short", 80.0, 40.0, 80.0) == 1.0


def test_atr_adaptive_step_clamps_to_configured_bounds():
    assert atr_adaptive_step(0.004, 0.001, True, 0.6, 0.0008, 0.01) == 0.0008
    assert atr_adaptive_step(0.004, 0.006, True, 0.6, 0.0008, 0.01) == pytest.approx(0.0036)
    assert atr_adaptive_step(0.004, 0.05, True, 0.6, 0.0008, 0.01) == 0.01
    assert atr_adaptive_step(0.004, 0.006, False, 0.6, 0.0008, 0.01) == 0.004


def test_hard_hedge_order_qty_targets_soft_limit_and_respects_cap():
    qty = hard_hedge_order_qty(
        360.0,
        150.0,
        300.0,
        price=0.18,
        contract_value=1.0,
        qty_step=1.0,
        min_qty=1.0,
        max_order_notional_usdt=100.0,
    )
    assert qty == 556
    assert hard_hedge_order_qty(299.0, 150.0, 300.0, 0.18, 1.0, 1.0, 1.0, 100.0) == 0.0


def test_grid_range_blocks_the_side_that_would_add_breakout_inventory():
    strategy = bare_strategy()
    strategy._grid_range_enabled = True
    strategy._grid_lower_price = 90.0
    strategy._grid_upper_price = 110.0
    strategy._grid_boundary_buffer_ratio = 0.0
    strategy._last_price = 111.0
    assert strategy._grid_range_side_block_reason("short") == "grid_range_upper_boundary"
    assert strategy._grid_range_side_block_reason("long") == ""
    strategy._last_price = 89.0
    assert strategy._grid_range_side_block_reason("long") == "grid_range_lower_boundary"
    assert strategy._grid_range_side_block_reason("short") == ""


def test_main_does_not_pass_encrypted_password_directly():
    source = (ROOT / 'main.py').read_text(encoding='utf-8')
    assert 'from config_manager import load_api_password_secure' in source
    assert 'pw = load_api_password_secure()' in source
    assert 'web_cfg.get("api_password")' not in source
    assert 'OKX_KEY_PASSWORD' in source
    assert 'OKX_API_PASSWORD' not in source

def test_futures_strategy_uses_single_trade_gateway():
    # 2026-10-18 拆分后：策略代码分布在 futures_hedge.py + strategies/hedge_kit/*.py，
    # 守卫改为对整个策略包生效（约束实质不变：任何策略文件都不得直连交易所网关）。
    parts = [(ROOT / 'strategies' / 'futures_hedge.py').read_text(encoding='utf-8')]
    kit_dir = ROOT / 'strategies' / 'hedge_kit'
    if kit_dir.exists():
        for f in sorted(kit_dir.glob('*.py')):
            parts.append(f.read_text(encoding='utf-8'))
    source = '\n'.join(parts)
    exec_source = (ROOT / 'strategies' / 'hedge_execution.py').read_text(encoding='utf-8')
    assert source.count('self._client.place_order(') == 0
    assert source.count('self._client.set_leverage(') == 0
    assert source.count('self._client._call(self._client._trade.cancel_order') == 0
    # 拆分第五阶段后，网关调用收敛到 hedge_execution.py 执行适配模块；
    # 策略文件只保留兼容代理，不直接触达网关。
    assert 'gateway.place_order(' in exec_source
    assert 'gateway.cancel_order(' in exec_source
    assert 'gateway.set_leverage(' in exec_source
    assert 'def _submit_order(' in source
    assert 'def _cancel_order(' in source
    assert 'def _set_leverage(' in source


def test_tp_supplement_stops_at_hard_ceiling_without_order():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._max_tp_orders = 2
    strategy._max_tp_orders_hard = 3
    strategy._active_tp_orders = 3
    strategy._long = Pos('long', entry=100.0, qty=10.0, cost=10.0)
    calls = []

    def place(lid, side, qty, price):
        calls.append((lid, side, qty, price))
        strategy._layer_tp_orders[lid] = 'safety-oid'

    strategy._place_tp_order = place
    created = strategy._supplement_tp_coverage_side('long', strategy._long)

    assert created == 1
    assert len(calls) == 1
    assert calls[0][2] == 10.0
    assert strategy._active_tp_orders == 4
    assert strategy._tp_covered_qty('long') == 10.0


def test_tp_supplement_allows_only_one_safety_order_above_soft_cap():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._max_tp_orders = 2
    strategy._max_tp_orders_hard = 3
    strategy._active_tp_orders = 2
    strategy._long = Pos('long', entry=100.0, qty=10.0, cost=10.0)
    calls = []

    def place(lid, side, qty, price):
        calls.append((lid, side, qty, price))
        strategy._layer_tp_orders[lid] = 'oid-1'

    strategy._place_tp_order = place
    created = strategy._supplement_tp_coverage_side('long', strategy._long)

    assert created == 1
    assert len(calls) == 1
    assert strategy._active_tp_orders == 3
    assert strategy._tp_covered_qty('long') == 10.0


def test_tp_supplement_does_not_split_one_lot_into_three_orders():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._max_tp_orders = 40
    strategy._max_tp_orders_hard = 50
    strategy._active_tp_orders = 0
    strategy._long = Pos('long', entry=100.0, qty=1.0, cost=1.0)
    calls = []

    def place(lid, side, qty, price):
        calls.append((lid, side, qty, price))
        strategy._layer_tp_orders[lid] = 'one-lot-oid'

    strategy._place_tp_order = place
    created = strategy._supplement_tp_coverage_side('long', strategy._long)

    assert created == 1
    assert len(calls) == 1
    assert calls[0][2] == 1.0
    assert strategy._tp_covered_qty('long') == 1.0


def test_tp_supplement_uses_distinct_exchange_ticks_for_split_gap():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._max_tp_orders = 40
    strategy._max_tp_orders_hard = 50
    strategy._active_tp_orders = 0
    strategy._long = Pos('long', entry=100.0, qty=2.0, cost=2.0)
    calls = []

    def place(lid, side, qty, price):
        calls.append((lid, side, qty, price))
        strategy._layer_tp_orders[lid] = f'oid-{len(calls)}'
        return True

    strategy._place_tp_order = place
    created = strategy._supplement_tp_coverage_side('long', strategy._long)

    assert created == 2
    assert len({call[3] for call in calls}) == 2
    assert calls[1][3] > calls[0][3]


def test_tp_supplement_skips_uneconomic_dust_outside_stale_band():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._max_tp_orders = 40
    strategy._max_tp_orders_hard = 50
    strategy._active_tp_orders = 0
    strategy._last_price = 0.21
    strategy._tick_size = 0.0001
    strategy._short = Pos('short', entry=0.21, qty=1.0, cost=0.21)
    calls = []
    strategy._submit_order = lambda *args, **kwargs: calls.append((args, kwargs)) or {'ordId': 'dust'}

    created = strategy._supplement_tp_coverage_side('short', strategy._short)

    assert created == 0
    assert calls == []
    assert strategy._layer_tp_orders == {}


def test_tp_supplement_stops_after_first_failed_placement():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._max_tp_orders = 40
    strategy._max_tp_orders_hard = 50
    strategy._active_tp_orders = 0
    strategy._long = Pos('long', entry=100.0, qty=10.0, cost=10.0)
    calls = []
    strategy._place_tp_order = lambda *args: calls.append(args)

    created = strategy._supplement_tp_coverage_side('long', strategy._long)

    assert created == 0
    assert len(calls) == 1


def test_fart_coverage_safety_tp_repairs_small_unprofitable_residual():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._symbol = 'FARTCOIN-USDT-SWAP'
    strategy._tick_size = 0.0001
    strategy._max_tp_orders = 34
    strategy._max_tp_orders_hard = 50
    strategy._active_tp_orders = 2
    strategy._coverage_safety_tp_enabled = True
    strategy._coverage_safety_tp_max_gap_ratio = 0.10
    strategy._coverage_safety_tp_max_loss_usdt = 0.10
    strategy._short = Pos('short', entry=0.125611, qty=1532.0, cost=192.5)
    strategy._layer_tps = {'short-1': 0.1253}
    strategy._layer_qty_map = {'short-1': 1437.0}
    strategy._layer_side_map = {'short-1': 'short'}
    strategy._layer_tp_orders = {'short-1': 'existing-oid'}
    calls = []

    def place(lid, side, qty, price, **kwargs):
        calls.append((lid, side, qty, price, kwargs))
        strategy._layer_tp_orders[lid] = 'safety-oid'
        return True

    strategy._place_tp_order = place
    created = strategy._supplement_tp_coverage_side('short', strategy._short)

    assert created == 1
    assert calls[0][1] == 'short'
    assert calls[0][2] == 95.0
    assert abs(calls[0][3] - 0.1254) < 1e-9
    assert calls[0][4] == {'coverage_safety': True}
    assert strategy._layer_qty_map[calls[0][0]] == 95.0


def test_coverage_safety_tp_is_disabled_by_default_and_for_large_gaps():
    strategy = bare_strategy()
    strategy._short = Pos('short', entry=100.0, qty=100.0, cost=100.0)
    assert strategy._coverage_safety_tp_allowed('short', 5.0, 99.9, strategy._short) is False
    strategy._coverage_safety_tp_enabled = True
    strategy._coverage_safety_tp_max_gap_ratio = 0.10
    strategy._coverage_safety_tp_max_loss_usdt = 0.01
    assert strategy._coverage_safety_tp_allowed('short', 11.0, 99.9, strategy._short) is False


def compaction_client(live, position_snapshots, cancel_fail=None):
    get_orders = object()
    cancel_order = object()
    get_positions = object()
    cancelled = set()
    placed = []
    position_reads = []

    def call(method, **kwargs):
        if method is get_orders:
            return {
                'code': '0',
                'data': [order for order in live if order['ordId'] not in cancelled],
            }
        if method is cancel_order:
            if kwargs['ordId'] == cancel_fail:
                raise RuntimeError('cancel failed')
            cancelled.add(kwargs['ordId'])
            return {'code': '0', 'data': [{'sCode': '0'}]}
        if method is get_positions:
            index = min(len(position_reads), len(position_snapshots) - 1)
            position_reads.append(index)
            return {'code': '0', 'data': position_snapshots[index]}
        raise AssertionError('unexpected API call')

    def place_order(_symbol, side, qty, ord_type, px, pos_side, reduce_only):
        oid = f'new-{len(placed)}'
        order = {
            'ordId': oid,
            'reduceOnly': 'true' if reduce_only else 'false',
            'posSide': pos_side,
            'side': side,
            'px': str(px),
            'sz': str(qty),
        }
        live.append(order)
        placed.append(order)
        return {'ordId': oid}

    client = SimpleNamespace(
        _trade=SimpleNamespace(get_order_list=get_orders, cancel_order=cancel_order),
        _account=SimpleNamespace(get_positions=get_positions),
        _call=call,
        place_order=place_order,
    )
    return client, cancelled, placed, position_reads


def test_tp_compaction_preserves_nearest_orders_and_expected_net_profit():
    strategy = bare_strategy()
    strategy._long = Pos('long', entry=100.0, qty=6.0, cost=10.0)
    strategy._short = Pos('short', entry=100.0, qty=6.0, cost=10.0)
    strategy._last_price = 100.0
    strategy._max_tp_orders = 5
    strategy._max_tp_orders_hard = 7
    live = [
        {'ordId': 'long-near', 'reduceOnly': 'true', 'posSide': 'long', 'px': '101', 'sz': '2'},
        {'ordId': 'long-mid', 'reduceOnly': 'true', 'posSide': 'long', 'px': '103', 'sz': '2'},
        {'ordId': 'long-far', 'reduceOnly': 'true', 'posSide': 'long', 'px': '105', 'sz': '2'},
        {'ordId': 'short-near', 'reduceOnly': 'true', 'posSide': 'short', 'px': '99', 'sz': '2'},
        {'ordId': 'short-mid', 'reduceOnly': 'true', 'posSide': 'short', 'px': '97', 'sz': '2'},
        {'ordId': 'short-far', 'reduceOnly': 'true', 'posSide': 'short', 'px': '95', 'sz': '2'},
    ]
    snapshots = [
        [
            {'pos': '6', 'posSide': 'long', 'avgPx': '100'},
            {'pos': '6', 'posSide': 'short', 'avgPx': '100'},
        ],
        [
            {'pos': '6', 'posSide': 'long', 'avgPx': '100.2'},
            {'pos': '6', 'posSide': 'short', 'avgPx': '99.8'},
        ],
    ]
    strategy._client, cancelled, placed, position_reads = compaction_client(live, snapshots)

    final_orders = strategy._compact_fragmented_tp_orders(live[:], 'soft_cap_6_5')

    assert final_orders is not None
    assert {'long-near', 'short-near'}.isdisjoint(cancelled)
    assert cancelled == {'long-mid', 'long-far', 'short-mid', 'short-far'}
    assert len([order for order in final_orders if order['reduceOnly'] == 'true']) == 5
    assert len(position_reads) >= 3
    assert strategy._long.entry == 100.2
    assert strategy._short.entry == 99.8
    for side, entry in (('long', 100.2), ('short', 99.8)):
        old = [order for order in live[:6] if order['posSide'] == side]
        final = [order for order in final_orders if order['posSide'] == side]
        assert sum(float(order['sz']) for order in final) == 6.0
        assert strategy._tp_orders_expected_net(side, final, entry) + 1e-8 >= strategy._tp_orders_expected_net(side, old, entry)
    assert all(
        strategy._tp_expected_net(order['posSide'], float(order['sz']), float(order['px']), 100.2 if order['posSide'] == 'long' else 99.8)
        >= strategy._min_expected_net_usdt - 1e-8
        for order in placed
    )


def test_tp_compaction_keeps_uncancelled_remote_order_without_overcoverage():
    strategy = bare_strategy()
    strategy._long = Pos('long', entry=100.0, qty=6.0, cost=10.0)
    strategy._short = Pos('short', entry=100.0, qty=6.0, cost=10.0)
    strategy._last_price = 100.0
    strategy._max_tp_orders = 5
    live = [
        {'ordId': 'long-near', 'reduceOnly': 'true', 'posSide': 'long', 'px': '101', 'sz': '2'},
        {'ordId': 'long-mid', 'reduceOnly': 'true', 'posSide': 'long', 'px': '103', 'sz': '2'},
        {'ordId': 'long-far', 'reduceOnly': 'true', 'posSide': 'long', 'px': '105', 'sz': '2'},
        {'ordId': 'short-near', 'reduceOnly': 'true', 'posSide': 'short', 'px': '99', 'sz': '2'},
        {'ordId': 'short-mid', 'reduceOnly': 'true', 'posSide': 'short', 'px': '97', 'sz': '2'},
        {'ordId': 'short-far', 'reduceOnly': 'true', 'posSide': 'short', 'px': '95', 'sz': '2'},
    ]
    snapshots = [[
        {'pos': '6', 'posSide': 'long', 'avgPx': '100'},
        {'pos': '6', 'posSide': 'short', 'avgPx': '100'},
    ]]
    strategy._client, cancelled, _placed, _reads = compaction_client(
        live, snapshots, cancel_fail='long-far'
    )

    final_orders = strategy._compact_fragmented_tp_orders(live[:], 'soft_cap_6_5')

    assert final_orders is not None
    assert 'long-far' not in cancelled
    assert any(order['ordId'] == 'long-far' for order in final_orders)
    assert len(final_orders) == 5
    for side in ('long', 'short'):
        assert sum(float(order['sz']) for order in final_orders if order['posSide'] == side) == 6.0


def test_tp_bucket_profit_floor_covers_fees_funding_and_buffer():
    strategy = bare_strategy()
    for side, source in (
        ('long', [{'sz': '2', 'px': '100.2'}, {'sz': '3', 'px': '100.3'}]),
        ('short', [{'sz': '2', 'px': '99.8'}, {'sz': '3', 'px': '99.7'}]),
    ):
        buckets = strategy._build_remote_tp_buckets(side, source, 5.0, 1, 100.0)
        assert sum(order['sz'] for order in buckets) == 5.0
        assert strategy._tp_orders_expected_net(side, buckets, 100.0) + 1e-8 >= strategy._tp_orders_expected_net(side, source, 100.0)
        assert strategy._tp_orders_expected_net(side, buckets, 100.0) >= strategy._min_expected_net_usdt - 1e-8


def test_tp_reconcile_does_not_seed_when_live_order_read_fails():
    strategy = bare_strategy()
    strategy._long = Pos('long', entry=100.0, qty=10.0, cost=10.0)
    strategy._max_tp_orders = 5
    strategy._tp_init_done = True
    get_orders = object()
    strategy._client = SimpleNamespace(
        _trade=SimpleNamespace(get_order_list=get_orders),
        _call=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('read failed')),
    )
    placed = []
    strategy._place_tp_order = lambda *args: placed.append(args)

    strategy._init_existing_tp(cancel_existing=False)

    assert strategy._tp_init_done is False
    assert placed == []


def tp_orders(count, side='long', price=110.0):
    return [
        {'ordId': f'{side}-{i}', 'reduceOnly': 'true', 'posSide': side, 'px': str(price), 'sz': '1'}
        for i in range(count)
    ]


def compaction_strategy():
    strategy = bare_strategy()
    strategy._max_tp_orders = 40
    strategy._last_price = 100.0
    strategy._fill_sync_ts_ms = 0
    strategy._last_position_change_ts = 0.0
    strategy._last_tp_compact_ts = 0.0
    strategy._tp_compact_candidate_signature = None
    strategy._tp_compact_candidate_since = 0.0
    strategy._trading_halted = False
    return strategy


def test_normal_tp_compaction_requires_stable_snapshot():
    strategy = compaction_strategy()
    orders = tp_orders(5, 'long', 110.0) + tp_orders(5, 'short', 90.0)

    assert strategy._tp_compaction_reason(orders) == ''
    strategy._tp_compact_candidate_since -= 31
    assert strategy._tp_compaction_reason(orders) == 'count_10'


def test_tp_compaction_triggers_for_one_sided_or_duplicate_fragmentation():
    for orders, expected in (
        (tp_orders(7), 'side_7'),
        (tp_orders(3), 'duplicate_3'),
    ):
        strategy = compaction_strategy()
        assert strategy._tp_compaction_reason(orders) == ''
        strategy._tp_compact_candidate_since -= 31
        assert strategy._tp_compaction_reason(orders) == expected


def test_tp_compaction_defers_recent_activity_but_can_protect_near_market_orders():
    strategy = compaction_strategy()
    orders = tp_orders(10)
    strategy._last_position_change_ts = __import__('time').time()
    assert strategy._tp_compaction_reason(orders) == ''
    assert strategy._tp_compact_candidate_signature is None

    strategy._last_position_change_ts = 0.0
    strategy._fill_sync_ts_ms = int(__import__('time').time() * 1000)
    assert strategy._tp_compaction_reason(orders) == ''
    assert strategy._tp_compact_candidate_signature is None

    strategy._fill_sync_ts_ms = 0
    assert strategy._tp_compaction_reason(tp_orders(10, price=100.1)) == ''
    strategy._tp_compact_candidate_since -= 31
    assert strategy._tp_compaction_reason(tp_orders(10, price=100.1)) == 'count_10'


def test_soft_cap_tp_compaction_bypasses_normal_delays():
    strategy = compaction_strategy()
    strategy._max_tp_orders = 2
    strategy._last_position_change_ts = __import__('time').time()

    assert strategy._tp_compaction_reason(tp_orders(3)) == 'soft_cap_3_2'


def test_position_sync_reconciles_tp_once_after_both_sides_grow():
    strategy = bare_strategy()
    strategy._symbol = 'TEST-USDT-SWAP'
    strategy._cur_lev = 2
    strategy._long = Pos('long', entry=100.0, qty=1.0, cost=50.0)
    strategy._short = Pos('short', entry=100.0, qty=1.0, cost=50.0)
    strategy._position_missing_counts = {'long': 0, 'short': 0}
    response = {'data': [
        {'pos': '3', 'posSide': 'long', 'avgPx': '101'},
        {'pos': '4', 'posSide': 'short', 'avgPx': '99'},
    ]}
    strategy._client = SimpleNamespace(
        _account=SimpleNamespace(get_positions=object()),
        _call=lambda *_args, **_kwargs: response,
    )
    strategy._update_position_age = lambda *_args: None
    strategy._estimate_adds = lambda *_args: 1
    reconciles = []
    strategy._init_existing_tp = lambda cancel_existing=False: reconciles.append(cancel_existing)
    strategy._clear_side_runtime_state = lambda *_args, **_kwargs: None

    strategy._sync_positions()

    assert strategy._long.qty == 3.0
    assert strategy._short.qty == 4.0
    assert reconciles == [False]


def test_net_exposure_side_blocks_only_dominant_side():
    strategy = bare_strategy()
    strategy._max_net_exposure = 5
    strategy._long.qty = 20
    strategy._short.qty = 3
    assert strategy._net_exposure_side_blocks() == (True, False)
    strategy._long.qty = 2
    strategy._short.qty = 20
    assert strategy._net_exposure_side_blocks() == (False, True)
    strategy._long.qty = 7
    strategy._short.qty = 3
    assert strategy._net_exposure_side_blocks() == (False, False)


def test_progressive_rebalance_is_removed_from_the_strategy_surface():
    strategy = bare_strategy()
    strategy._client = SimpleNamespace(place_order=lambda *args, **kwargs: {'ordId': 'x'})
    strategy._trading_halted = False
    strategy._net_reduce_cooldown = 0
    strategy._rebalance_cooldown_sec = 90
    strategy._rebalance_trigger_ratio = 0.20
    strategy._rebalance_reduce_ratio = 0.25
    strategy._rebalance_max_chunk_pct = 0.18
    strategy._cur_lev = 2
    strategy._cached_equity = 1000.0
    strategy._max_margin_ratio = 0.55
    strategy._base_pos_usdt = 50.0
    strategy._last_price = 53.0
    strategy._long = Pos('long', entry=50.0, qty=20.0, cost=1000.0)
    strategy._short = Pos('short', entry=53.0, qty=2.0, cost=106.0)
    strategy._dominant_side = lambda: 'long'
    strategy._trend_counter_side = lambda: 'short'
    strategy._net_imbalance_ratio = lambda: 0.55
    strategy._rebalance_targets = lambda: (0.20, 0.01)
    strategy._account_margin_load_ratio = lambda: 0.10
    strategy._order_succeeded = lambda *_args, **_kwargs: True
    calls = []
    strategy._client.place_order = lambda *args, **kwargs: calls.append((args, kwargs)) or {'ordId': 'x'}

    assert not hasattr(strategy, '_check_progressive_rebalance')
    assert calls == []
    assert strategy._long.qty == 20.0
    assert strategy._short.qty == 2.0


def test_instrument_info_replaces_unsafe_default_contract_step():
    strategy = bare_strategy()

    strategy.update_info({'ok': True, 'minSz': 0.1, 'lotSz': 0.1, 'tickSz': 0.01, 'ctVal': 1})

    assert strategy._min_qty == 0.1
    assert strategy._qty_step == 0.1
    assert strategy._tick_size == 0.01
    assert strategy._ct_val == 1.0
    assert strategy._instrument_info_ready is True


def test_fast_market_is_observed_without_pausing_or_cancelling():
    strategy = bare_strategy()
    now = __import__('time').time()
    strategy._price_history = [
        (now - 899 + i * 60, 100.0 if i < 9 else 103.0)
        for i in range(10)
    ]
    strategy._circuit_breaker_cd = 1800
    cancelled = []
    strategy._cancel_opening_orders = lambda: cancelled.append(True)

    result = strategy._check_circuit_breaker(105.1)

    assert result is None
    assert cancelled == []
    assert strategy._cb_pause_until > now
    assert strategy._opening_order_block_reason('long', 1.0, 105.1) == ''


def test_moca_is_monitor_only_until_live_application_is_validated():
    strategy = bare_strategy()
    strategy._moca_apply_live = False
    strategy._moca_state = ''
    strategy._moca_observed_state = ''
    strategy._moca_observed_confidence = 0.0

    strategy.update_moca({'state': 'trending_down', 'confidence': 0.91})

    assert strategy._moca_state == ''
    assert strategy._moca_observed_state == 'trending_down'
    assert strategy._moca_observed_confidence == 0.91


def test_okx_candles_are_returned_in_chronological_order():
    client = OKXClient.__new__(OKXClient)
    client._market = SimpleNamespace(get_candlesticks=object())
    client._call = lambda *_args, **_kwargs: {'data': [
        ['2000', '2', '3', '1', '2.5', '20'],
        ['1000', '1', '2', '0.5', '1.5', '10'],
    ]}

    candles = client.get_candles('TEST-USDT-SWAP', limit=2)

    assert [candle['ts'] for candle in candles] == [1000, 2000]


def test_opening_guard_blocks_dominant_side_but_allows_net_reduction():
    strategy = bare_strategy()
    strategy._client = SimpleNamespace(
        _trade=SimpleNamespace(get_order_list=object()),
        _call=lambda *_args, **_kwargs: {'data': []},
    )
    strategy._last_price = 52.0
    strategy._max_side_notional_usdt = 250.0
    strategy._max_net_notional_usdt = 150.0
    strategy._long = Pos('long', entry=53.0, qty=11.0, cost=291.5)
    strategy._short = Pos('short', entry=52.0, qty=1.0, cost=26.0)

    assert strategy._opening_order_block_reason('long', 0.1, 52.0) == 'max_side_notional'
    assert strategy._opening_order_block_reason('short', 0.1, 52.0) == 'max_total_notional'
    assert strategy._opening_order_block_reason(
        'short', 0.1, 52.0, inventory_repair=True
    ) == ''


def test_inventory_repair_can_cross_soft_side_cap_when_margin_is_low():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._fetch_live_orders = lambda: []
    strategy._cached_equity = 1000.0
    strategy._account_margin_load_ratio = lambda: 0.20
    strategy._max_side_notional_usdt = 250.0
    strategy._max_net_notional_usdt = 150.0
    strategy._dynamic_side_notional_enabled = False
    strategy._inventory_repair_side_cap_multiplier = 1.15
    strategy._inventory_repair_max_margin_ratio = 0.35
    strategy._long = Pos('long', entry=53.0, qty=10.0, cost=265.0)
    strategy._short = Pos('short', entry=52.0, qty=4.7, cost=122.2)

    assert strategy._opening_order_block_reason('short', 0.5, 52.0) == 'max_side_notional'
    assert strategy._opening_order_block_reason(
        'short', 0.5, 52.0, inventory_repair=True
    ) == ''


def test_dynamic_side_cap_expands_with_equity_only_at_low_margin():
    strategy = bare_strategy()
    strategy._max_side_notional_usdt = 250.0
    strategy._dynamic_side_notional_enabled = True
    strategy._dynamic_side_notional_equity_ratio = 0.20
    strategy._dynamic_side_notional_max_multiplier = 1.40
    strategy._dynamic_side_notional_max_margin_ratio = 0.35
    strategy._cached_equity = 1725.0
    strategy._cached_frozen = 290.0

    first_cap = strategy._effective_max_side_notional_usdt()
    assert 250.0 < first_cap <= 345.0

    strategy._cached_frozen = 700.0
    assert 250.0 <= strategy._effective_max_side_notional_usdt() < first_cap


def test_dynamic_side_cap_still_obeys_hard_net_notional_limit():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._fetch_live_orders = lambda: []
    strategy._cached_equity = 1725.0
    strategy._cached_frozen = 290.0
    strategy._dynamic_side_notional_enabled = True
    strategy._dynamic_side_notional_equity_ratio = 0.20
    strategy._dynamic_side_notional_max_multiplier = 1.40
    strategy._dynamic_side_notional_max_margin_ratio = 0.35
    # Isolate the hard net cap from the independent side cap so this test
    # verifies that dynamic sizing cannot bypass directional exposure.
    strategy._max_side_notional_usdt = 400.0
    strategy._max_net_notional_usdt = 150.0
    strategy._long = Pos('long', entry=53.0, qty=5.0, cost=132.5)
    strategy._short = Pos('short', entry=52.0, qty=2.5, cost=65.0)

    assert strategy._opening_order_block_reason('long', 0.5, 53.0) == 'max_net_notional'


def test_dram_style_dynamic_cap_allows_balanced_grid_expansion():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._fetch_live_orders = lambda: []
    strategy._cached_equity = 1725.0
    strategy._cached_frozen = 290.0
    strategy._dynamic_side_notional_enabled = True
    strategy._dynamic_side_notional_equity_ratio = 0.20
    strategy._dynamic_side_notional_max_multiplier = 1.40
    strategy._dynamic_side_notional_max_margin_ratio = 0.35
    strategy._max_side_notional_usdt = 250.0
    strategy._max_net_notional_usdt = 150.0
    strategy._long = Pos('long', entry=53.0, qty=5.0, cost=132.5)
    strategy._short = Pos('short', entry=52.0, qty=5.2, cost=135.2)

    assert strategy._opening_order_block_reason('long', 0.5, 53.0) == ''
    assert strategy._opening_order_block_reason('short', 0.5, 53.0) == ''


def test_optional_side_loss_and_inventory_age_controls_can_be_disabled():
    import time

    strategy = bare_strategy()
    strategy._recent_side_loss_guard_enabled = False
    strategy._recent_side_loss_until = {'long': time.time() + 1800, 'short': time.time() + 1800}
    strategy._inventory_age_sizing_enabled = False
    strategy._position_first_seen = {'long': time.time() - 86400, 'short': time.time() - 86400}
    strategy._last_price = 90.0
    strategy._long = Pos('long', entry=100.0, qty=5.0, cost=250.0)

    assert strategy._recent_side_loss_active('long') is False
    assert strategy._inventory_age_multiplier('long') == 1.0


def test_pending_inventory_repair_order_prevents_extra_side_cap_exception():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._fetch_live_orders = lambda: [
        {'reduceOnly': 'false', 'posSide': 'long', 'sz': '0.5'},
    ]
    strategy._cached_equity = 1000.0
    strategy._account_margin_load_ratio = lambda: 0.20
    strategy._max_side_notional_usdt = 250.0
    strategy._max_net_notional_usdt = 150.0
    strategy._dynamic_side_notional_enabled = False
    strategy._inventory_repair_side_cap_multiplier = 1.15
    strategy._inventory_repair_max_margin_ratio = 0.35
    strategy._long = Pos('long', entry=53.0, qty=4.0, cost=106.0)
    strategy._short = Pos('short', entry=52.0, qty=4.7, cost=122.2)

    assert strategy._opening_order_block_reason('long', 0.5, 52.5) == 'max_side_notional'


def test_engine_action_submission_cannot_bypass_opening_guard():
    calls = []
    client = SimpleNamespace(place_order=lambda *args, **kwargs: calls.append((args, kwargs)))
    strategy = SimpleNamespace(
        _client=client,
        _symbol='DRAM-USDT-SWAP',
        _opening_order_block_reason=lambda *_args: 'max_side_notional',
    )
    engine = MainEngine.__new__(MainEngine)
    engine.client = client
    engine.log = LogStub()

    result = engine._submit_strategy_action(strategy, {
        'action': 'buy',
        'quantity': '0.1',
        'pos_side': 'long',
        'order_type': 'limit',
        'price': '52.0',
    })

    assert result['sCode'] == 'opening_guard'
    assert calls == []


def test_live_profiles_have_reachable_imbalance_and_explicit_notional_caps():
    import yaml

    for relative in ('config/config.yaml', 'config/config_dram.yaml', 'config/config_fartcoin.yaml'):
        path = ROOT / relative
        if not path.exists():
            # 已停用/退役的 profile（如 DRAM）不再参与实盘校验
            continue
        futures = yaml.safe_load(path.read_text(encoding='utf-8'))['futures']
        assert 0 < float(futures['grid_bias_ratio']) < 1
        assert float(futures['max_side_notional_usdt']) > 0
        assert float(futures['max_net_notional_usdt']) > 0


def api_client_for_rate_tests():
    client = OKXClient.__new__(OKXClient)
    client.log = LogStub()
    client._lock = threading.Lock()
    client._call_times = []
    client._retries = 3
    client._retry_delay = 1
    client._rate_limit_window = 2.0
    client._rate_limit_max_calls = 8
    client._rate_backoff_until = 0.0
    client._rate_limit_failures = 0
    now = [0.0]
    client._clock = lambda: now[0]
    client._sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
    return client, now


def test_okx_client_retries_rate_limit_with_shared_backoff():
    client, now = api_client_for_rate_tests()
    responses = [
        {'code': '50011', 'msg': 'Too Many Requests'},
        {'code': '0', 'data': []},
    ]

    result = client._call(lambda: responses.pop(0))

    assert result['code'] == '0'
    assert now[0] >= 2.0
    assert client._rate_limit_failures == 0


def test_okx_client_keeps_backoff_after_final_rate_limit_response():
    client, now = api_client_for_rate_tests()

    result = client._call(lambda: {'code': '50011', 'msg': 'Too Many Requests'})

    assert result['code'] == '50011'
    assert client._rate_backoff_until > now[0]


def test_okx_client_spreads_burst_calls_across_window():
    client, now = api_client_for_rate_tests()
    client._rate_limit_max_calls = 2
    endpoint = lambda: {'code': '0', 'data': []}

    client._call(endpoint)
    client._call(endpoint)
    client._call(endpoint)

    assert now[0] >= 2.0


def test_order_list_error_is_not_reported_as_empty_snapshot():
    client = OKXClient.__new__(OKXClient)
    client._trade = SimpleNamespace(get_order_list=object())
    client._call = lambda *_args, **_kwargs: {
        'code': '50011', 'msg': 'Too Many Requests', 'data': [],
    }

    try:
        client.get_order_list('TEST-USDT-SWAP')
    except RuntimeError as exc:
        assert 'order query failed' in str(exc)
    else:
        raise AssertionError('failed order query must not look like an empty order book')


def test_grid_refill_fails_closed_when_live_orders_are_unknown():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._trading_halted = False
    strategy._last_price = 100.0
    strategy._max_net_exposure = 500
    strategy._in_startup_recovery = lambda: False
    strategy._net_exposure_side_blocks = lambda: (False, False)
    strategy._target_grid_layers = lambda: 2
    strategy._fetch_live_orders = lambda: (_ for _ in ()).throw(RuntimeError('order state unavailable'))
    events = []
    strategy._record_event = lambda *args, **kwargs: events.append((args, kwargs))
    strategy._place_open_order = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('must not place from an unknown live-order snapshot')
    )

    strategy._place_initial_grid()

    assert events[-1][1]['reason'] == 'live_order_state_unavailable'


def test_grid_refill_stops_each_side_after_deterministic_guard_reject():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._trading_halted = False
    strategy._last_price = 100.0
    strategy._max_net_exposure = 500
    strategy._in_startup_recovery = lambda: False
    strategy._net_exposure_side_blocks = lambda: (False, False)
    strategy._target_grid_layers = lambda: 3
    strategy._fetch_live_orders = lambda: []
    strategy._grid_refill_block_reason = lambda _side: ''
    strategy._log_grid_refill_block = lambda *_args: None
    strategy._effective_base_pos_usdt = lambda _side=None: 50.0
    strategy._grid_pause_active = lambda _side: False
    strategy._has_expected_edge = lambda *_args: (True, '')
    strategy._execution_guard = None
    strategy._long_grid_levels = []
    strategy._short_grid_levels = []
    strategy._last_short_grid_fail_px = 0.0
    strategy._last_short_grid_fail_ts = 0.0
    strategy._last_short_grid_px = 0.0
    strategy._last_short_grid_ts = 0.0
    strategy._short_grid_fail_cooldown = 45.0
    calls = []
    pauses = []
    strategy._record_event = lambda *_args, **_kwargs: None
    strategy._pause_grid_side = lambda side, seconds, reason='': pauses.append((side, seconds, reason))
    strategy._place_open_order = lambda *args, **kwargs: (
        calls.append((args, kwargs)) or
        {'sCode': 'opening_guard', 'sMsg': 'max_side_notional', 'guard_blocked': True}
    )

    strategy._place_initial_grid()

    assert [call[0][2] for call in calls] == ['long', 'short']
    assert pauses == [
        ('long', 45.0, 'max_side_notional'),
        ('short', 45.0, 'max_side_notional'),
    ]


def test_grid_refill_preserves_exchange_grid_levels_for_drift_tracking():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._trading_halted = False
    strategy._last_price = 100.0
    strategy._max_net_exposure = 500
    strategy._in_startup_recovery = lambda: False
    strategy._net_exposure_side_blocks = lambda: (False, False)
    strategy._target_grid_layers = lambda: 1
    strategy._fetch_live_orders = lambda: [
        {'reduceOnly': 'false', 'side': 'buy', 'posSide': 'long', 'px': '98.0', 'sz': '1'},
        {'reduceOnly': 'false', 'side': 'sell', 'posSide': 'short', 'px': '102.0', 'sz': '1'},
    ]
    strategy._grid_refill_block_reason = lambda _side: ''
    strategy._log_grid_refill_block = lambda *_args: None
    strategy._effective_base_pos_usdt = lambda _side=None: 50.0
    strategy._long_grid_levels = []
    strategy._short_grid_levels = []
    strategy._record_event = lambda *_args, **_kwargs: None

    strategy._place_initial_grid()

    assert strategy._long_grid_levels == [98.0]
    assert strategy._short_grid_levels == [102.0]


def test_reduce_only_blocks_new_orders_without_disabling_strategy():
    strategy = bare_strategy()
    strategy._reduce_only_mode = True
    strategy._running = True
    strategy._enabled = True

    assert strategy._opening_order_block_reason('long', 1.0, 100.0) == 'reduce_only_mode'
    assert strategy.is_running is True
    assert strategy.is_enabled is True


def test_reduce_only_cancels_opening_orders_but_keeps_tp_orders():
    strategy = bare_strategy()
    strategy._client = object()
    strategy._long_grid_levels = [99.0]
    strategy._short_grid_levels = [101.0]
    strategy._layer_tp_orders = {'tp': 'tp-1'}
    strategy._fetch_live_orders = lambda: [
        {'ordId': 'grid-1', 'reduceOnly': 'false'},
        {'ordId': 'tp-1', 'reduceOnly': 'true'},
    ]
    cancelled = []
    strategy._cancel_order = lambda oid: cancelled.append(oid) or True

    count = strategy._cancel_opening_orders()

    assert count == 1
    assert cancelled == ['grid-1']
    assert strategy._layer_tp_orders == {'tp': 'tp-1'}
    assert strategy._long_grid_levels == []
    assert strategy._short_grid_levels == []


def test_enabling_reduce_only_immediately_cancels_opening_orders():
    strategy = bare_strategy()
    strategy._reduce_only_mode = False
    strategy._last_reduce_only_cancel = 0.0
    calls = []
    strategy._cancel_opening_orders = lambda: calls.append(True) or 0

    strategy.set_reduce_only(True)

    assert strategy._reduce_only_mode is True
    assert strategy._last_reduce_only_cancel > 0
    assert calls == [True]


def test_dram_paired_add_can_use_bounded_margin_override_without_reopening_grid():
    strategy = bare_strategy()
    strategy._symbol = "DRAM-USDT-SWAP"
    strategy._paired_average_down_enabled = True
    strategy._paired_add_allow_inventory_pause = True
    strategy._paired_add_max_margin_ratio = 0.45
    strategy._account_margin_load_ratio = lambda: 0.43
    strategy._inventory_margin_paused = True
    strategy._active_inventory_repair_enabled = True
    strategy._paired_add_enabled = True
    strategy._inventory_margin_thresholds = lambda: (0.46, 0.41, 0.44)
    strategy._inventory_opening_margin_blocked = lambda *_args: True
    strategy._margin_first_capacity_ready = lambda: True
    strategy._long = Pos("long", qty=8.0)
    strategy._short = Pos("short", qty=22.0)

    assert strategy._paired_add_margin_allowed('short') is True

    strategy._account_margin_load_ratio = lambda: 0.46
    assert strategy._paired_add_margin_allowed('short') is False


def test_dram_triggered_pair_does_not_exclusively_block_normal_dominant_grid():
    strategy = bare_strategy()
    strategy._symbol = "DRAM-USDT-SWAP"
    strategy._paired_add_enabled = True
    strategy._hedged_grid_allow_dominant_refill = True
    strategy._margin_first_capacity_ready = lambda: True
    strategy._last_price = 55.0
    strategy._dominant_side = lambda: "short"
    strategy._paired_add_triggered = lambda side, price: side == "short" and price == 55.0
    strategy._grid_capacity_block_reason = lambda side: ""
    strategy._tp_coverage_block_reason = lambda side: ""
    strategy._mgn_ratio_opening_block_reason = lambda: ""
    strategy._grid_side_enabled = lambda side: True

    assert strategy._grid_refill_block_reason("short") == ""


def test_margin_first_grid_capacity_ignores_internal_gross_caps_when_budget_allows():
    strategy = bare_strategy()
    strategy._symbol = "DRAM-USDT-SWAP"
    strategy._last_price = 55.0
    strategy._long = Pos("long", qty=8.0)
    strategy._short = Pos("short", qty=22.0)
    strategy._dynamic_notional_limits = lambda: {"side_cap": 300.0, "long_cap": 300.0, "short_cap": 300.0, "total_cap": 600.0}
    strategy._margin_first_capacity_ready = lambda: True
    strategy._opening_order_block_reason = lambda *args, **kwargs: ""
    strategy._account_budget_allocator = None

    assert strategy._grid_capacity_qty("short", 2.0, 55.0) == 2.0


def test_dram_margin_band_keeps_grid_active_at_half_size_below_hard_limit():
    strategy = bare_strategy()
    strategy._symbol = "DRAM-USDT-SWAP"
    strategy._hedged_grid_allow_dominant_refill = True
    strategy._margin_first_capacity_enabled = True
    strategy._margin_first_max_margin_ratio = 0.55
    strategy._margin_first_normal_margin_ratio = 0.52
    strategy._margin_first_reduced_size_ratio = 0.5
    strategy._account_margin_data_block_reason = lambda: ""
    strategy._mgn_ratio_opening_block_reason = lambda: ""
    strategy._account_margin_load_ratio = lambda: 0.53
    strategy._base_pos_usdt = 20.0

    assert strategy._margin_first_capacity_ready() is True
    assert strategy._hedged_dominant_grid_allowed() is True
    assert strategy._effective_base_pos_usdt("short") == 10.0


def test_dram_net_cap_covers_existing_inventory_plus_two_maker_layers():
    import yaml
    dram_cfg_path = ROOT / "config" / "config_dram.yaml"
    if not dram_cfg_path.exists():
        pytest.skip("config_dram.yaml 已停用（DRAM 策略退役）")
    cfg = yaml.safe_load(dram_cfg_path.read_text(encoding="utf-8"))
    futures = cfg["futures"]
    assert futures["max_net_notional_usdt"] >= 900
    assert futures["max_net_notional_usdt"] > 790.83 + 2 * 0.7 * 54.54


def test_crv_can_opt_in_to_bounded_pair_activity_during_inventory_pause():
    strategy = bare_strategy()
    strategy._symbol = "CRV-USDT-SWAP"
    strategy._paired_add_enabled = True
    strategy._paired_average_down_enabled = True
    strategy._paired_add_allow_inventory_pause = True
    strategy._paired_add_max_margin_ratio = 0.46
    strategy._inventory_margin_paused = True
    strategy._account_margin_load_ratio = lambda: 0.452
    strategy._inventory_opening_margin_blocked = lambda *_args: True
    strategy._margin_first_capacity_ready = lambda: True

    assert strategy._paired_add_margin_allowed('short') is True

    strategy._paired_add_allow_inventory_pause = False
    assert strategy._paired_add_margin_allowed('short') is False


def test_crv_over_cap_is_soft_capacity_not_market_unwind():
    from strategies.futures_hedge import FuturesHedgeStrategy
    strategy = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    strategy._symbol = "CRV-USDT-SWAP"
    strategy._over_cap_unwind_enabled = True
    strategy._client = object()
    strategy._account_margin_load_ratio = lambda: 0.54
    strategy._account_margin_hard_limit = 0.55
    strategy._mgn_ratio_opening_block_reason = lambda: ""
    strategy._account_margin_data_block_reason = lambda: ""
    strategy._over_cap_unwind_last_reason = ""
    result = strategy._maybe_over_cap_unwind(0.204)
    assert result is None
    assert strategy._over_cap_unwind_last_reason == "soft_capacity_only"
