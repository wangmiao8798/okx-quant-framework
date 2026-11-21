import json
from pathlib import Path

from web.customer_metrics import build_customer_metrics


def test_customer_metrics_reads_only_strategy_directory(tmp_path):
    user_dir = tmp_path / 'u7_s9'
    user_dir.mkdir()
    (user_dir / 'fee_pnl_crv.json').write_text(json.dumps({
        'total_realized_pnl': 12.5,
        'total_fees_paid': 1.25,
        'total_funding': -0.2,
        'cached_equity': 101.0,
        'cached_avail': 80.0,
        'last_update': 'now',
    }))
    (user_dir / 'pnl_records.csv').write_text(
        'time,symbol,side,qty,entry,exit,gross,fee,net\n'
        '2026-07-16 10:00:00,CRV-USDT-SWAP,long,2,1,1.1,0.2,0.01,0.19\n'
    )
    result = build_customer_metrics(user_dir, 'CRV-USDT-SWAP')
    assert result['data_available'] is True
    assert result['realized_pnl'] == 12.5
    assert result['total_fees'] == 1.25
    assert result['funding_pnl'] == -0.2
    assert result['closed_trades'] == 1
    assert result['net_closed_pnl'] == 0.19
    assert result['research']['advisory_only'] is True
    assert result['research']['status'] == 'collecting'


def test_customer_metrics_does_not_fallback_to_global_files(tmp_path):
    global_file = tmp_path / 'fee_pnl.json'
    global_file.write_text(json.dumps({'total_realized_pnl': 999}))
    result = build_customer_metrics(tmp_path / 'missing', 'DRAM-USDT-SWAP')
    assert result['data_available'] is False
    assert result['realized_pnl'] is None
    assert result['total_fees'] is None
    assert result['research']['status'] == 'no_data'
