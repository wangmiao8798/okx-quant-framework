import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path('/opt/okx_quant/scripts/dram_cross_market_monitor.py')
spec = importlib.util.spec_from_file_location('dram_cross_market_monitor', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_parse_nasdaq_quote_uses_primary_and_regular_close():
    payload = {
        'data': {
            'symbol': 'DRAM',
            'marketStatus': 'After-Hours',
            'primaryData': {
                'lastSalePrice': '$61.91',
                'netChange': '+0.76',
                'percentageChange': '+1.24%',
                'lastTradeTimestamp': 'Jul 14, 2026 7:40 PM ET',
                'bidPrice': '$61.86',
                'askPrice': '$62.19',
            },
            'secondaryData': {
                'lastSalePrice': '$61.15',
                'netChange': '+3.85',
                'percentageChange': '+6.72%',
                'lastTradeTimestamp': 'Closed at Jul 14, 2026 4:00 PM ET',
            },
        }
    }
    quote = module.parse_nasdaq_quote(payload)
    assert quote['ok'] is True
    assert quote['symbol'] == 'DRAM'
    assert quote['last'] == 61.91
    assert quote['change_pct'] == 1.24
    assert quote['regular_close'] == 61.15
    assert quote['regular_change_pct'] == 6.72
    assert quote['market_status'] == 'After-Hours'


def test_parse_naver_quote_prefers_visible_nxt_session():
    html = '''
    <em class="date">2026.07.15 12:54</em>
    <a href="javascript:showStockExchangeArea('rate_info_krx', 'rate_info_nxt')">tab</a>
    <div class="rate_info" id="rate_info_krx" style="display: none;">
      <p class="no_today"><span class="blind">263,000</span></p>
      <span class="blind">0</span><span class="blind">0</span>
    </div>
    <div class="rate_info" id="rate_info_nxt" style="display: block;">
      <em class="no_up">상승</em>
      <p class="no_today"><span class="blind">278,500</span></p>
      <span class="blind">15,500</span><span class="blind">5.89</span>
    </div>
    '''
    quote = module.parse_naver_quote(html, '005930', 'Samsung Electronics')
    assert quote['ok'] is True
    assert quote['session'] == 'NXT'
    assert quote['last'] == 278500.0
    assert quote['change'] == 15500.0
    assert quote['change_pct'] == 5.89
    assert quote['quote_ts_ms'] == 1784087640000
    assert quote['quote_time'] == '2026.07.15 12:54'


def test_parse_naver_quote_uses_nonzero_nxt_when_visible_krx_is_stale():
    html = '''
    <div class="rate_info" id="rate_info_krx" style="display: block;">
      <p class="no_today"><span class="blind">1,913,000</span></p>
      <span class="blind">0</span><span class="blind">0.00</span>
    </div>
    <div class="rate_info" id="rate_info_nxt" style="display: none;">
      <em class="no_up">상승</em>
      <p class="no_today"><span class="blind">2,102,000</span></p>
      <span class="blind">189,000</span><span class="blind">9.88</span>
    </div>
    '''
    quote = module.parse_naver_quote(html, '000660', 'SK Hynix')
    assert quote['session'] == 'NXT'
    assert quote['last'] == 2102000.0
    assert quote['change_pct'] == 9.88


def test_build_signal_uses_live_etf_mid_and_okx_mid():
    now_ms = 2_000_000
    result = module.build_signal(
        now_ms=now_ms,
        etf={'ok': True, 'bid': 61.86, 'ask': 62.10, 'last': 61.91, 'market_status': 'After-Hours', 'fetched_at_ms': now_ms - 1_000},
        okx={'ok': True, 'bid': 62.05, 'ask': 62.06, 'index': 62.05, 'mark': 62.06, 'fetched_at_ms': now_ms - 500},
        components=[
            {'ok': True, 'symbol': 'MU', 'regular_change_pct': 4.92},
            {'ok': True, 'symbol': '005930', 'change_pct': 5.89},
            {'ok': True, 'symbol': '000660', 'change_pct': 8.99},
        ],
        max_age_ms=60_000,
    )
    expected = (((62.05 + 62.06) / 2) / ((61.86 + 62.10) / 2) - 1) * 100
    assert abs(result['deviation_pct'] - expected) < 1e-6
    assert result['direction'] in {'neutral', 'okx_premium', 'okx_discount'}
    assert result['data_fresh'] is True
    assert result['component_breadth']['positive'] == 3


def test_reference_price_uses_last_when_etf_spread_is_too_wide():
    etf = {'bid': 60.63, 'ask': 62.58, 'last': 62.2288}
    assert module._reference_price(etf) == 62.2288


def test_reference_price_uses_mid_when_etf_spread_is_tight():
    etf = {'bid': 61.86, 'ask': 61.88, 'last': 61.87}
    assert abs(module._reference_price(etf) - 61.87) < 1e-9


def test_build_signal_closed_market_keeps_deviation_but_forces_neutral():
    now=2_000_000
    result=module.build_signal(
        now_ms=now,
        etf={'ok':True,'last':60.0,'market_status':'Closed','fetched_at_ms':now},
        okx={'ok':True,'mid':62.0,'fetched_at_ms':now},
        components=[],max_age_ms=60_000,
    )
    assert result['deviation_pct'] > 0
    assert result['direction'] == 'neutral'
    assert 'equity_market_closed' in result['reasons']


def test_directional_forecast_requires_confirmed_reference_momentum():
    result = module.classify_directional_forecast(
        session="korea",
        reference_returns={5: -1.4, 15: -2.1},
        okx_returns={5: -0.2, 15: -0.4},
        reference_valid=True,
    )
    assert result["bias"] == "down"
    assert result["confidence"] >= 60
    assert "reference_leads_down" in result["reasons"]


def test_directional_forecast_rejects_conflicting_windows():
    result = module.classify_directional_forecast(
        session="korea",
        reference_returns={5: 1.2, 15: -1.0},
        okx_returns={5: 1.0, 15: -0.8},
        reference_valid=True,
    )
    assert result["bias"] == "neutral"
    assert "conflicting_reference_momentum" in result["reasons"]


def test_directional_forecast_rejects_stale_or_already_led_move():
    stale = module.classify_directional_forecast(
        session="us",
        reference_returns={5: -2.0, 15: -3.0},
        okx_returns={5: -2.2, 15: -3.4},
        reference_valid=False,
    )
    already_led = module.classify_directional_forecast(
        session="us",
        reference_returns={5: -1.0, 15: -1.5},
        okx_returns={5: -1.8, 15: -2.8},
        reference_valid=True,
    )
    assert stale["bias"] == "neutral"
    assert "reference_not_fresh" in stale["reasons"]
    assert already_led["bias"] == "neutral"
    assert "okx_already_leads" in already_led["reasons"]


def test_build_signal_goes_neutral_when_reference_is_stale():
    result = module.build_signal(
        now_ms=1_000_000,
        etf={'ok': True, 'last': 60.0, 'market_status': 'Closed', 'fetched_at_ms': 100_000},
        okx={'ok': True, 'bid': 62.0, 'ask': 62.1, 'fetched_at_ms': 999_000},
        components=[],
        max_age_ms=60_000,
    )
    assert result['data_fresh'] is False
    assert result['direction'] == 'neutral'
    assert result['confidence'] == 0
    assert 'stale_reference' in result['reasons']


def test_closed_reference_sessions_skip_before_network(monkeypatch):
    monkeypatch.setattr(module, 'fetch_nasdaq_quote', lambda *_args: (_ for _ in ()).throw(AssertionError('network called')))
    result = module.collect(at=datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc))
    assert result['skip_reason'] == 'outside_reference_sessions'


def test_korea_session_does_not_fetch_us_references(monkeypatch):
    now = datetime(2026, 7, 15, 3, 0, tzinfo=timezone.utc); now_ms = int(now.timestamp() * 1000)
    monkeypatch.setattr(module, 'fetch_nasdaq_quote', lambda *_args: (_ for _ in ()).throw(AssertionError('US fetch called')))
    monkeypatch.setattr(module, 'fetch_naver_quote', lambda code, name: {
        'ok': True, 'symbol': code, 'name': name, 'last': 100.0, 'change_pct': 1.0,
        'session': 'KRX', 'quote_ts_ms': now_ms, 'fetched_at_ms': now_ms,
    })
    monkeypatch.setattr(module, 'fetch_okx_snapshot', lambda: {
        'ok': True, 'last': 52.0, 'mid': 52.0, 'fetched_at_ms': now_ms,
    })

    result = module.collect(at=now)

    assert result['active_session'] == 'korea'
    assert result['quotes']['dram_etf']['skipped'] == 'us_session_closed'
