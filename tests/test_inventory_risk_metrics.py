import json
import sqlite3

from scripts.sync_trading_data import _ensure_schema, sync_strategy_status
from scripts.oscillation_harvest_report import inventory_history_metrics, harvest_coverage_metrics


def test_sync_strategy_status_writes_all_profiles():
    conn = sqlite3.connect(':memory:')
    _ensure_schema(conn)
    status = {
        'updated_at': 100,
        'strategies': {
            'futures': {'symbol': 'CRV-USDT-SWAP', 'long_qty': 10, 'short_qty': 8},
            'futures_dram': {'symbol': 'DRAM-USDT-SWAP', 'long_qty': 3, 'short_qty': 1},
            'futures_fartcoin': {'symbol': 'FARTCOIN-USDT-SWAP', 'long_qty': 4, 'short_qty': 7},
        },
    }
    assert sync_strategy_status(conn, status) == 3
    rows = conn.execute('select strategy,state from strategy_status order by strategy').fetchall()
    assert [row[0] for row in rows] == ['futures', 'futures_dram', 'futures_fartcoin']
    assert json.loads(rows[1][1])['symbol'] == 'DRAM-USDT-SWAP'


def test_inventory_metrics_report_growth_and_skew_duration():
    samples = [
        {'ts': 0, 'long_qty': 10, 'short_qty': 10},
        {'ts': 48 * 3600, 'long_qty': 30, 'short_qty': 10},
        {'ts': 66 * 3600, 'long_qty': 34, 'short_qty': 10},
        {'ts': 72 * 3600, 'long_qty': 40, 'short_qty': 10},
    ]
    result = inventory_history_metrics(samples, now_sec=72 * 3600)
    assert result['change_6h'] == {'long': 6.0, 'short': 0.0, 'net': 6.0}
    assert result['change_24h'] == {'long': 10.0, 'short': 0.0, 'net': 10.0}
    assert result['change_72h'] == {'long': 30.0, 'short': 0.0, 'net': 30.0}
    assert result['skew_over_50h'] >= 24.0


def test_harvest_coverage_compares_realized_with_open_loss():
    result = harvest_coverage_metrics(realized_net=15.0, unrealized_pnl=-10.0)
    assert result['harvest_to_open_loss_ratio'] == 1.5
    assert result['open_loss_covered_by_harvest'] is True
    assert result['harvest_surplus_after_open_loss'] == 5.0
