import sqlite3

from scripts.full_market_scanner import build_report, persist_scan, score_candidate


def base(**overrides):
    item = {
        'notional_m': 100.0,
        'change24': 2.0,
        'funding': 0.00005,
        'spread_pct': 0.02,
        'rsi15': 55.0,
        'rsi1h': 55.0,
        'rsi4h': 60.0,
        'ema20_15': 0.3,
        'ema50_15': 0.1,
        'ema20_1h': 1.0,
        'ema50_1h': 0.4,
        'ema20_4h': 2.0,
        'ema50_4h': 1.0,
        'vol_ratio15': 1.5,
        'book_imbalance': 0.25,
        'candle_age_sec_15': 120.0,
        'candle_age_sec_1h': 120.0,
        'candle_age_sec_4h': 120.0,
        'candle_ts_15': 1700000000000,
        'round_trip_cost_pct': 0.12,
        'trend_strength_pct': 3.0,
    }
    item.update(overrides)
    return item


def test_long_score_requires_multi_timeframe_alignment():
    result = score_candidate(base(), 'long')
    assert result['eligible'] is True
    assert result['score'] >= 7
    assert result['direction'] == 'long'


def test_overheated_long_is_rejected():
    result = score_candidate(base(rsi1h=88.0, rsi4h=84.0, change24=14.0), 'long')
    assert result['eligible'] is False
    assert 'overheated' in result['reasons']


def test_short_score_rejects_strong_uptrend():
    result = score_candidate(base(), 'short')
    assert result['eligible'] is False
    assert 'timeframe_misalignment' in result['reasons']


def test_thin_market_is_rejected():
    result = score_candidate(base(notional_m=5.0), 'long')
    assert result['eligible'] is False
    assert 'thin_liquidity' in result['reasons']


def test_short_timeframe_misalignment_is_rejected():
    result = score_candidate(base(ema20_15=-0.2, ema50_15=-0.1), 'long')
    assert result['eligible'] is False
    assert 'timeframe_misalignment' in result['reasons']


def test_adverse_funding_is_rejected():
    result = score_candidate(base(funding=0.0008), 'long')
    assert result['eligible'] is False
    assert 'adverse_funding' in result['reasons']


def test_signal_has_deduplication_key_and_expiry():
    result = score_candidate(base(), 'long')
    assert result['signal_key'] == 'None:1700000000000:long' or result['signal_key'].endswith(':long')
    assert result['valid_until_ts'] == 1700001800000


def test_persistence_and_report_capture_net_outcomes(tmp_path):
    item = base(symbol='ETH-USDT-SWAP', last=100.0, signal_key_base='ETH-USDT-SWAP:1')
    item['long'] = score_candidate(item, 'long')
    item['short'] = score_candidate(item, 'short')
    db_path = tmp_path / 'scanner.db'
    persist_scan(str(db_path), 1700000000000, 10, 1, [], [item], {item['long']['signal_key']})

    conn = sqlite3.connect(db_path)
    conn.execute(
        'INSERT INTO scanner_outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (item['long']['signal_key'], '15m', 1700000900000, 0.0, 101.0, 1.0, 0.8, 'test'),
    )
    conn.commit()
    conn.close()

    report = build_report(str(db_path))
    assert report['eligible_signals'] == 1
    assert report['horizons']['15m']['samples'] == 1
    assert report['horizons']['15m']['profit_factor'] == 'inf'
