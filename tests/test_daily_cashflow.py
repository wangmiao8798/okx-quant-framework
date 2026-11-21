import datetime as dt

from scripts.daily_profit_report import _cashflow_window, completed_day_window, format_cashflow_summary


def test_completed_day_window_excludes_the_current_partial_day():
    start, end = completed_day_window(dt.datetime(2026, 8, 8, 8, 10))
    assert start == dt.datetime(2026, 8, 7, 0, 0)
    assert end == dt.datetime(2026, 8, 8, 0, 0)


def test_cashflow_window_is_start_inclusive_and_end_exclusive_per_symbol():
    start = int(dt.datetime(2026, 8, 7, 0, 0).timestamp() * 1000)
    end = int(dt.datetime(2026, 8, 8, 0, 0).timestamp() * 1000)
    total, by_symbol = _cashflow_window([
        {'ts': str(start), 'instId': 'CRV-USDT-SWAP', 'balChg': '1.25'},
        {'ts': str(end - 1), 'instId': 'DRAM-USDT-SWAP', 'balChg': '-0.50'},
        {'ts': str(end), 'instId': 'FARTCOIN-USDT-SWAP', 'balChg': '9.99'},
    ], start, end)
    assert total == 0.75
    assert by_symbol == {
        'CRV-USDT-SWAP': 1.25,
        'DRAM-USDT-SWAP': -0.5,
        'FARTCOIN-USDT-SWAP': 0.0,
    }


def test_cashflow_summary_labels_completed_day_and_intraday_separately():
    text = format_cashflow_summary({
        'completed_day': '2026-08-07',
        'completed_day_cashflow': 5.30,
        'intraday_cashflow': 1.25,
        'rolling_24h_cashflow': 62.88,
        'cash_balance': 2067.55,
        'equity': 1865.16,
        'unrealized': -202.39,
        'data_complete': True,
    })
    assert '上一自然日（2026-08-07）已实现现金流 +5.30U' in text
    assert '今日盘中现金流 +1.25U' in text
    assert '滚动24h现金流 +62.88U' in text
    assert '余额 2067.55U' in text
    assert '权益 1865.16U' in text
    assert '浮亏 -202.39U' in text
    assert '含手续费和资金费' in text
