import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

MODULE = Path('/opt/okx_quant/scripts/cross_market_opportunity_radar.py')
spec = importlib.util.spec_from_file_location('cross_market_opportunity_radar', MODULE)
radar = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(radar)


def make_config(tmp_path):
    path = tmp_path / 'assets.yaml'
    path.write_text(yaml.safe_dump({
        'public_read_only': True, 'advisory_only': True,
        'quality': {'min_equities': 2, 'max_age_seconds': 300},
        'thresholds': {'lead_pct': 2.0},
        'themes': {'btc_equities': {'crypto': 'BTC-USDT-SWAP', 'equities': ['IBIT', 'MSTR'], 'benchmark': 'QQQ'}}
    }), encoding='utf-8')
    return path


def q(symbol, change, *, age=0, ok=True, market='Market Open', window=None):
    row = {'symbol': symbol, 'price': 100.0, 'change_pct': change, 'age_seconds': age, 'ok': ok, 'market_status': market}
    if window is not None: row['window_change_pct'] = window
    return row


def test_load_config_and_public_quote_parsers(tmp_path):
    cfg = radar.load_config(make_config(tmp_path))
    assert cfg['themes']['btc_equities']['crypto'] == 'BTC-USDT-SWAP'
    okx = radar.parse_okx_ticker({'instId': 'BTC-USDT-SWAP', 'last': '65000', 'open24h': '62500', 'ts': '1000000'}, 1001000)
    assert okx['price'] == 65000.0
    assert abs(okx['change_pct'] - 4.0) < 1e-9
    nasdaq = radar.parse_nasdaq_quote({'data': {'symbol': 'IBIT', 'marketStatus': 'Closed', 'primaryData': {'lastSalePrice': '$50.00', 'percentageChange': '+2.50%', 'lastTradeTimestamp': 'Jul 14, 2026'}}}, 1000)
    assert nasdaq['price'] == 50.0 and nasdaq['market_status'] == 'Closed'


def test_missing_okx_timestamp_fails_closed():
    row = radar.parse_okx_ticker({'instId': 'BTC-USDT-SWAP', 'last': '65000', 'open24h': '62500'}, 1001000)
    assert row['ok'] is False
    assert row['age_seconds'] > 300


def test_only_aligned_window_can_generate_direction(tmp_path):
    cfg = radar.load_config(make_config(tmp_path))
    base = {'BTC-USDT-SWAP': q('BTC-USDT-SWAP', 5, window=5), 'QQQ': q('QQQ', 1, window=1), 'IBIT': q('IBIT', 2, window=2), 'MSTR': q('MSTR', 2, window=2)}
    lead = radar.evaluate_theme('btc_equities', cfg, base, '2026-01-01T00:00:00+00:00')
    assert lead['signal'] == 'crypto_leads' and lead['divergence_pct'] == 4.0
    base['BTC-USDT-SWAP']['window_change_pct'] = -3
    assert radar.evaluate_theme('btc_equities', cfg, base, 'x')['signal'] == 'equities_lead'
    base['BTC-USDT-SWAP']['window_change_pct'] = 1
    assert radar.evaluate_theme('btc_equities', cfg, base, 'x')['signal'] == 'neutral'


def test_mixed_windows_and_closed_market_force_neutral(tmp_path):
    cfg = radar.load_config(make_config(tmp_path))
    mixed = {'BTC-USDT-SWAP': q('BTC-USDT-SWAP', 5), 'QQQ': q('QQQ', 1), 'IBIT': q('IBIT', 2), 'MSTR': q('MSTR', 2)}
    result = radar.evaluate_theme('btc_equities', cfg, mixed, 'x')
    assert result['signal'] == 'neutral' and 'unaligned_return_window' in result['reasons']
    for symbol in ('QQQ', 'IBIT', 'MSTR'): mixed[symbol]['market_status'] = 'Closed'
    result = radar.evaluate_theme('btc_equities', cfg, mixed, 'x')
    assert result['signal'] == 'neutral' and 'equity_market_closed' in result['reasons']


def test_stale_or_missing_is_neutral(tmp_path):
    cfg = radar.load_config(make_config(tmp_path))
    stale = {'BTC-USDT-SWAP': q('BTC-USDT-SWAP', 5, window=5), 'QQQ': q('QQQ', 1, age=301, window=1), 'IBIT': q('IBIT', 2, window=2), 'MSTR': q('MSTR', 2, window=2)}
    assert 'stale_benchmark' in radar.evaluate_theme('btc_equities', cfg, stale, 'x')['reasons']
    missing = {'BTC-USDT-SWAP': q('BTC-USDT-SWAP', 5, window=5), 'QQQ': q('QQQ', 1, window=1), 'IBIT': q('IBIT', 2, window=2)}
    assert 'insufficient_equities' in radar.evaluate_theme('btc_equities', cfg, missing, 'x')['reasons']


def test_sqlite_schema_and_idempotent_explicit_columns(tmp_path):
    db = tmp_path / 'radar.db'
    quotes = [q('BTC-USDT-SWAP', 1)]
    themes = [{'theme': 'btc_equities', 'captured_at': '2026-01-01T00:00:00+00:00', 'signal': 'neutral', 'advisory_only': True, 'reasons': ['inside_band'], 'data_quality': 'ok'}]
    radar.save_results(db, 'batch-1', quotes, themes)
    radar.save_results(db, 'batch-1', quotes, themes)
    with sqlite3.connect(db) as conn:
        assert conn.execute('select count(*) from normalized_quotes').fetchone()[0] == 1
        assert conn.execute('select count(*) from theme_snapshots').fetchone()[0] == 1
        assert conn.execute('select count(*) from shadow_signals').fetchone()[0] == 1
        assert conn.execute('select batch_id from theme_snapshots').fetchone()[0] == 'batch-1'
    with sqlite3.connect(db) as conn:
        conn.execute('alter table theme_snapshots rename to broken')
        conn.execute('create table theme_snapshots(x text)')
    with pytest.raises(RuntimeError, match='incompatible schema'):
        radar.save_results(db, 'batch-2', quotes, themes)


def test_summary_is_compact():
    text = radar.compact_summary([{'theme': 'btc_equities', 'signal': 'neutral', 'divergence_pct': 2.1, 'data_quality': 'bad'}])
    assert 'btc_equities=neutral' in text and len(text) < 220


def test_collection_skips_closed_session_before_network(monkeypatch, tmp_path):
    cfg = radar.load_config(make_config(tmp_path))
    monkeypatch.setattr(radar, 'fetch_nasdaq', lambda *_args: (_ for _ in ()).throw(AssertionError('network called')))

    result = radar.collect(cfg, at=datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc))

    assert result['skipped'] is True
    assert result['skip_reason'] == 'outside_us_regular_session'


def test_collection_checks_only_benchmark_on_market_holiday(monkeypatch, tmp_path):
    cfg = radar.load_config(make_config(tmp_path)); calls = []
    def fetch(symbol):
        calls.append(symbol)
        return q(symbol, 0, market='Closed')
    monkeypatch.setattr(radar, 'fetch_nasdaq', fetch)

    result = radar.collect(cfg, at=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc))

    assert result['skip_reason'] == 'us_reference_market_closed'
    assert calls == ['QQQ']
