#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT / 'data' / 'trading.db'
STATUS_PATH = PROJECT / 'data' / 'strategies_status.json'
BUDGET_PLAN_PATH = PROJECT / 'data' / 'portfolio_budget_plan.json'
BUDGET_APPLY_STATE_PATH = PROJECT / 'data' / 'portfolio_budget_apply_state.json'
QUOTE_WIDTH_PATH = PROJECT / 'data' / 'dynamic_quote_width_report.json'
PARAM_FEEDBACK_PATH = PROJECT / 'data' / 'parameter_feedback_report.json'
HARVEST_REPORT_PATH = PROJECT / 'data' / 'oscillation_harvest_report.json'
FEE_FILES = [PROJECT / 'data' / 'fee_pnl.json', PROJECT / 'data' / 'fee_pnl_dram.json', PROJECT / 'data' / 'fee_pnl_fartcoin.json']
TRACKED_SYMBOLS = ('CRV-USDT-SWAP', 'DRAM-USDT-SWAP', 'FARTCOIN-USDT-SWAP')
MAX_ACCOUNT_BILL_PAGES = 100


def ts_ms(value):
    return int(value.timestamp() * 1000)


def fmt_money(value):
    if value is None:
        return 'n/a'
    try:
        return f'{float(value):.2f}'
    except Exception:
        return 'n/a'


def fmt_pct(value):
    if value is None:
        return 'n/a'
    try:
        return f'{float(value):.2f}%'
    except Exception:
        return 'n/a'


def safe_json(path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def first_last_equity(cur, start_ms, end_ms):
    first = cur.execute(
        'select ts,equity,usdt from equity where ts>=? and ts<? order by ts asc limit 1',
        (start_ms, end_ms),
    ).fetchone()
    last = cur.execute(
        'select ts,equity,usdt from equity where ts>=? and ts<? order by ts desc limit 1',
        (start_ms, end_ms),
    ).fetchone()
    stats = cur.execute(
        'select min(equity), max(equity), count(*) from equity where ts>=? and ts<?',
        (start_ms, end_ms),
    ).fetchone()
    return first, last, stats


def max_drawdown(equities):
    peak = None
    worst = 0.0
    for value in equities:
        if value is None:
            continue
        value = float(value)
        if peak is None or value > peak:
            peak = value
        if peak and peak > 0:
            worst = min(worst, (value - peak) / peak)
    return abs(worst) * 100


def latest_strategy_states(cur):
    latest_ts = cur.execute('select max(ts) from strategy_status').fetchone()[0]
    if latest_ts is None:
        return []
    rows = cur.execute('select strategy,state from strategy_status where ts=?', (latest_ts,)).fetchall()
    states = []
    for strategy, raw in rows:
        try:
            state = json.loads(raw)
        except Exception:
            state = {}
        states.append((strategy, state))
    return states


def trades_summary(cur, start_ms, end_ms):
    rows = cur.execute(
        'select symbol, side, pnl, fee, ts from trades where ts>=? and ts<? order by ts asc',
        (start_ms, end_ms),
    ).fetchall()
    by_symbol = {}
    for symbol, side, pnl, fee, _ in rows:
        item = by_symbol.setdefault(symbol or 'unknown', {'count': 0, 'pnl': 0.0, 'fee': 0.0, 'long': 0, 'short': 0})
        item['count'] += 1
        item['pnl'] += float(pnl or 0)
        item['fee'] += float(fee or 0)
        if side in ('long', 'short'):
            item[side] += 1
    return rows, by_symbol


def plain_strategy_line(state, completed_day_cashflow=None):
    symbol = str(state.get('symbol', '未知')).replace('-USDT-SWAP', '')
    monitor = state.get('monitor', {}) if isinstance(state.get('monitor'), dict) else {}
    intraday_realized = float(state.get('daily_pnl') or 0)
    unrealized = float(monitor.get('total_unrealized_pnl') or 0)
    imbalance = float(monitor.get('imbalance_pct') or 0)
    dominant = monitor.get('dominant_side')
    position = ('多偏重' if dominant == 'long' else '空偏重') if imbalance >= 40 else '基本均衡'
    status = '重点关注' if unrealized < -30 else ('有浮亏' if unrealized < 0 else '正常')
    completed_text = '账单不完整' if completed_day_cashflow is None else f'{completed_day_cashflow:+.2f}U'
    return (f'{symbol} | 上一自然日现金流 {completed_text} | '
            f'今日盘中已实现 {intraday_realized:+.2f}U | 浮亏 {unrealized:+.2f}U | {position}，{status}')


def plain_harvest_risk_line(symbol, item):
    name = str(symbol).replace('-USDT-SWAP', '')
    ratio = (item.get('harvest_coverage') or {}).get('harvest_to_open_loss_ratio')
    parts = [name, '收割覆盖浮亏' + ('数据不足' if ratio is None else f'{float(ratio) * 100:.0f}%')]
    net = (item.get('inventory_history') or {}).get('change_24h', {}).get('net')
    if net is not None and abs(float(net)) >= 10:
        parts.append(f'24h偏重{float(net):+.0f}张')
    return ' | '.join(parts)


def plain_overall_summary(equity_delta, daily_realized, unrealized, margin_pct, halted_count):
    state = '正常' if not halted_count else f'{halted_count}个策略保护'
    risk = '高' if margin_pct >= 50 or unrealized < -100 else ('中' if margin_pct >= 40 or unrealized < -50 else '低')
    return (f'结论：{state}，风险{risk}。已实现 {daily_realized:+.2f}U，浮亏 {unrealized:+.2f}U，'
            f'净值变动 {equity_delta:+.2f}U（含非交易因素）；保证金 {margin_pct:.1f}%。')



def _make_okx_account_client():
    import sys
    sys.path[:0] = [str(PROJECT), str(PROJECT / "web")]
    import yaml
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    from config_manager import load_api_password_secure
    cfg = yaml.safe_load((PROJECT / "config" / "config.yaml").read_text()) or {}
    cm = CredentialManager(cfg)
    cm.set_password(load_api_password_secure())
    return OKXClient(cm.load(), flag=str(cfg.get("exchange", {}).get("okx", {}).get("flag", "0")))


def completed_day_window(now, days=1):
    """Return the most recent fully closed calendar-day window."""
    day_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_end - dt.timedelta(days=max(1, int(days)))
    return day_start, day_end


def _fetch_account_bills_since(client, begin_ms, max_pages=MAX_ACCOUNT_BILL_PAGES):
    rows = []
    after = ""
    complete = False
    for _ in range(max_pages):
        resp = client._call(client._account.get_account_bills, instType="SWAP", after=after, limit="100")
        if str(resp.get("code")) != "0":
            raise RuntimeError(f"account bills failed: {resp.get('code')} {resp.get('msg', '')}")
        page = resp.get("data", []) or []
        if not page:
            complete = True
            break
        rows.extend(page)
        oldest = min(int(float(x.get("ts") or 0)) for x in page)
        if oldest < begin_ms:
            complete = True
            break
        nxt = str(page[-1].get("billId") or "")
        if not nxt or nxt == after:
            break
        after = nxt
    return rows, complete


def _cashflow_window(rows, begin_ms, end_ms):
    by_symbol = {symbol: 0.0 for symbol in TRACKED_SYMBOLS}
    for row in rows:
        ts = int(float(row.get("ts") or 0))
        symbol = row.get("instId")
        if begin_ms <= ts < end_ms and symbol in by_symbol:
            by_symbol[symbol] += float(row.get("balChg") or 0)
    return round(sum(by_symbol.values()), 8), {symbol: round(value, 8) for symbol, value in by_symbol.items()}


def load_cashflow_snapshot(now=None, client=None):
    now = now or dt.datetime.now()
    completed_start, completed_end = completed_day_window(now)
    now_ms = ts_ms(now)
    completed_start_ms = ts_ms(completed_start)
    completed_end_ms = ts_ms(completed_end)
    client = client or _make_okx_account_client()
    rows, complete = _fetch_account_bills_since(client, completed_start_ms)
    completed_day, completed_by_symbol = _cashflow_window(rows, completed_start_ms, completed_end_ms)
    intraday, intraday_by_symbol = _cashflow_window(rows, completed_end_ms, now_ms)
    rolling, _rolling_by_symbol = _cashflow_window(rows, now_ms - 24 * 3600 * 1000, now_ms)
    resp = client._account.get_account_balance()
    if str(resp.get("code")) != "0":
        raise RuntimeError(f"account balance failed: {resp.get('code')} {resp.get('msg', '')}")
    details = (resp.get("data") or [{}])[0].get("details", [])
    usdt = next((x for x in details if x.get("ccy") == "USDT"), {})
    return {
        "completed_day": completed_start.date().isoformat(),
        "completed_day_cashflow": completed_day,
        "completed_day_by_symbol": completed_by_symbol,
        "completed_day_complete": complete,
        "intraday_cashflow": intraday,
        "intraday_by_symbol": intraday_by_symbol,
        "rolling_24h_cashflow": rolling,
        "cash_balance": float(usdt.get("cashBal") or 0),
        "equity": float(usdt.get("eq") or 0),
        "unrealized": float(usdt.get("upl") or 0),
        # Compatibility for consumers which only need the primary report state.
        "data_complete": complete,
    }


def format_cashflow_summary(snapshot):
    if not snapshot or not snapshot.get("data_complete"):
        return "完整日报现金流：OKX账单数据不可用或不完整，暂不下利润结论。"
    return (f"上一自然日（{snapshot['completed_day']}）已实现现金流 {snapshot['completed_day_cashflow']:+.2f}U；"
            f"今日盘中现金流 {snapshot['intraday_cashflow']:+.2f}U；"
            f"滚动24h现金流 {snapshot['rolling_24h_cashflow']:+.2f}U "
            f"（均含手续费和资金费）；余额 {snapshot['cash_balance']:.2f}U；权益 {snapshot['equity']:.2f}U；浮亏 {snapshot['unrealized']:+.2f}U。")

def build_report(days=1, cashflow_snapshot=None):
    now = dt.datetime.now()
    start, end = completed_day_window(now, days)
    start_ms, end_ms = ts_ms(start), ts_ms(end)
    lines = [f"量化运行简报｜{now.strftime('%Y-%m-%d %H:%M')}"]
    status = safe_json(STATUS_PATH) or {}
    states = [v for v in (status.get('strategies') or {}).values() if isinstance(v, dict)]

    equity_delta = 0.0
    equity_now = None
    available_now = None
    drawdown = 0.0
    if DB_PATH.exists():
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        first, last, _ = first_last_equity(cur, start_ms, end_ms)
        equities = [r[0] for r in cur.execute(
            'select equity from equity where ts>=? and ts<? order by ts asc',
            (start_ms, end_ms),
        ).fetchall()]
        if first and last:
            equity_delta = float(last[1] or 0) - float(first[1] or 0)
            equity_now = float(last[1] or 0)
            available_now = float(last[2] or 0)
            drawdown = max_drawdown(equities)
        con.close()

    intraday_realized = sum(float(s.get('daily_pnl') or 0) for s in states)
    unrealized = sum(float((s.get('monitor') or {}).get('total_unrealized_pnl') or 0) for s in states)
    margin_pct = max([float((s.get('monitor') or {}).get('margin_load_pct') or 0) for s in states] or [0])
    halted_count = int(status.get('halted_count') or 0)

    snapshot = cashflow_snapshot
    if snapshot is None:
        try:
            snapshot = load_cashflow_snapshot(now)
        except Exception as exc:
            snapshot = {"data_complete": False, "error": str(exc)}
    if snapshot.get("data_complete"):
        verdict = "正常" if not halted_count else f"{halted_count}个策略保护"
        risk = "高" if margin_pct >= 50 or float(snapshot.get("unrealized") or unrealized) < -100 else ("中" if margin_pct >= 40 or float(snapshot.get("unrealized") or unrealized) < -50 else "低")
        lines.append(f"结论（{snapshot['completed_day']}完整日报）：{verdict}，风险{risk}。已实现现金流 {snapshot['completed_day_cashflow']:+.2f}U；权益 {snapshot['equity']:.2f}U，浮亏 {snapshot['unrealized']:+.2f}U；保证金 {margin_pct:.1f}%。")
    else:
        lines.append(plain_overall_summary(equity_delta, intraday_realized, unrealized, margin_pct, halted_count) + "（完整日报账单不可用，已实现仅为今日盘中本地记录）")
    lines.append(format_cashflow_summary(snapshot))
    if equity_now is not None:
        lines.append(
            f"报告期 {start.strftime('%Y-%m-%d')}；期末账户约 {equity_now:.2f}U，可用约 {available_now:.2f}U；"
            f"报告期最大回撤约 {drawdown:.2f}%。"
        )

    harvest = safe_json(HARVEST_REPORT_PATH) or {}
    harvest_symbols = harvest.get('symbols') or {}
    lines.append('策略：')
    completed_by_symbol = snapshot.get('completed_day_by_symbol', {}) if snapshot.get('data_complete') else {}
    for state in sorted(states, key=lambda x: abs(float((x.get('monitor') or {}).get('total_unrealized_pnl') or 0)), reverse=True):
        lines.append(plain_strategy_line(state, completed_by_symbol.get(state.get('symbol'))))
    lines.append('处理优先级：')
    priority = []
    for state in states:
        monitor = state.get('monitor') or {}
        upnl = float(monitor.get('total_unrealized_pnl') or 0)
        if upnl >= 0:
            continue
        symbol = str(state.get('symbol', '')).replace('-USDT-SWAP', '')
        item = harvest_symbols.get(state.get('symbol'), {}) if isinstance(harvest_symbols, dict) else {}
        ratio = (item.get('harvest_coverage') or {}).get('harvest_to_open_loss_ratio') if isinstance(item, dict) else None
        priority.append((abs(upnl), symbol, upnl, ratio))
    for _, symbol, upnl, ratio in sorted(priority, reverse=True):
        cover = '数据不足' if ratio is None else f'收割覆盖{float(ratio) * 100:.0f}%浮亏'
        lines.append(f'- {symbol}：{cover}；控制偏重侧继续扩大；浮亏 {upnl:+.2f}U。')
    if not priority:
        lines.append('- 暂无需要优先处理的浮亏策略。')
    lines.append('口径：已实现=已平仓交易结果；浮亏=当前持仓未结算结果；净值变动不等于交易利润。')
    return '\n'.join(lines)



def append_control_loop_section(lines):
    budget = safe_json(BUDGET_PLAN_PATH) or {}
    apply_state = safe_json(BUDGET_APPLY_STATE_PATH) or {}
    quote = safe_json(QUOTE_WIDTH_PATH) or {}
    feedback = safe_json(PARAM_FEEDBACK_PATH) or {}
    status = safe_json(STATUS_PATH) or {}
    if not any([budget, apply_state, quote, feedback]):
        return
    quote_by_symbol = {r.get('symbol'): r for r in quote.get('symbols', []) if isinstance(r, dict)}
    feedback_by_symbol = {r.get('symbol'): r for r in feedback.get('symbols', []) if isinstance(r, dict)}
    monitor_by_symbol = {}
    for item in (status.get('strategies') or {}).values():
        if isinstance(item, dict):
            monitor_by_symbol[item.get('symbol')] = item.get('monitor', {}) if isinstance(item.get('monitor'), dict) else {}
    state_by_symbol = (apply_state.get('symbols') or {}) if isinstance(apply_state, dict) else {}
    rows = budget.get('symbols') or []
    if rows:
        lines.append('组合预算/执行闭环:')
    for row in rows:
        symbol = row.get('symbol', 'unknown')
        st = state_by_symbol.get(symbol, {}) if isinstance(state_by_symbol, dict) else {}
        q = quote_by_symbol.get(symbol, {})
        fb = feedback_by_symbol.get(symbol, {})
        mon = monitor_by_symbol.get(symbol, {})
        inv_l = mon.get('long_inventory_age_multiplier', 'n/a')
        inv_s = mon.get('short_inventory_age_multiplier', 'n/a')
        order_age = mon.get('order_active_max_age_sec', 'n/a')
        lines.append(
            f"- {symbol}: budget={row.get('action')} score={row.get('score')} 连续={st.get('consecutive', 0)} "
            f"quote={q.get('action', 'n/a')} feedback={fb.get('suggestion', 'n/a')} "
            f"margin={fmt_pct(row.get('margin_load_pct'))} upnl={fmt_money(row.get('unrealized'))} "
            f"order_age={order_age}s inv_mult=L{inv_l}/S{inv_s} "
            f"reasons={','.join(row.get('reasons', []))}"
        )
    if apply_state:
        lines.append('预算应用器: 当前为 dry-run 定时评估；未带 --apply，不会自动改配置。')

def main():
    parser = argparse.ArgumentParser(description='Generate read-only OKX quant profit report')
    parser.add_argument('--days', type=int, default=1)
    parser.add_argument('--output')
    args = parser.parse_args()
    report = build_report(max(1, args.days))
    if args.output:
        Path(args.output).write_text(report + '\n')
    print(report)


if __name__ == '__main__':
    main()
