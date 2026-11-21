from scripts.daily_profit_report import plain_strategy_line, plain_overall_summary, plain_harvest_risk_line


def test_plain_strategy_line_uses_everyday_chinese():
    state = {
        'symbol': 'DRAM-USDT-SWAP', 'running': True, 'halted': False,
        'daily_pnl': 1.25, 'pnl': 134.11, 'long_qty': 16, 'short_qty': 1,
        'monitor': {'total_unrealized_pnl': -18.37, 'imbalance_pct': 88.24,
                    'dominant_side': 'long', 'margin_load_pct': 34.45,
                    'long_unrealized_pnl': -18.37, 'short_unrealized_pnl': 0.0},
    }
    line = plain_strategy_line(state)
    assert 'DRAM' in line
    assert '已实现 +1.25U' in line
    assert '今天已落袋' not in line
    assert '浮亏 -18.37U' in line
    assert '多偏重' in line
    assert '有浮亏' in line
    assert all(word not in line for word in ('running=', 'halted=', 'pnl=', 'upnl=', 'margin='))


def test_plain_overall_summary_distinguishes_equity_from_profit():
    text = plain_overall_summary(37.91, 13.28, -31.08, 34.45, 0)
    assert '净值变动 +37.91U' in text
    assert '含非交易因素' in text
    assert '已实现 +13.28U' in text
    assert '浮亏 -31.08U' in text
    assert '结论：正常' in text


def test_plain_harvest_risk_line_warns_when_open_loss_not_covered():
    line = plain_harvest_risk_line('FARTCOIN-USDT-SWAP', {
        'harvest_coverage': {'harvest_to_open_loss_ratio': 0.52, 'open_loss_covered_by_harvest': False},
        'inventory_history': {'change_24h': {'long': 20, 'short': 0, 'net': 20}, 'history_hours': 30},
    })
    assert 'FARTCOIN' in line
    assert '收割覆盖浮亏52%' in line
    assert '24h偏重+20张' in line
    assert '24h偏重+20张' in line
