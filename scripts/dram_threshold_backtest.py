#!/usr/bin/env python3
"""DRAM threshold backtest and small parameter search.

Usage:
  python3 scripts/dram_threshold_backtest.py --csv data/dram_5m_recent.csv --threshold 60
  python3 scripts/dram_threshold_backtest.py --csv data/dram_5m_recent.csv --search
"""

from __future__ import annotations

import argparse
import csv
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Candle:
    ts: str
    close: float
    high: float | None = None
    low: float | None = None
    open: float | None = None


@dataclass
class TradeResult:
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str


def load_candles(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with path.open('r', encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            ts = row.get('time') or row.get('timestamp') or row.get('ts')
            close_raw = row.get('close') or row.get('c')
            if not ts or close_raw is None:
                continue
            candles.append(Candle(
                ts=str(ts),
                close=float(close_raw),
                high=float(row['high']) if row.get('high') else None,
                low=float(row['low']) if row.get('low') else None,
                open=float(row['open']) if row.get('open') else None,
            ))
    return candles


def run_backtest(candles: list[Candle], threshold: float, take_profit: float, stop_loss: float, max_hold_bars: int) -> list[TradeResult]:
    trades: list[TradeResult] = []
    position_entry: Optional[tuple[int, float]] = None

    for index, candle in enumerate(candles):
        if position_entry is None:
            if candle.close <= threshold:
                position_entry = (index, candle.close)
            continue

        entry_index, entry_price = position_entry
        pnl_pct = (candle.close - entry_price) / entry_price

        if pnl_pct >= take_profit:
            trades.append(TradeResult(entry_index, index, entry_price, candle.close, pnl_pct, 'take_profit'))
            position_entry = None
            continue
        if pnl_pct <= -stop_loss:
            trades.append(TradeResult(entry_index, index, entry_price, candle.close, pnl_pct, 'stop_loss'))
            position_entry = None
            continue
        if index - entry_index >= max_hold_bars:
            trades.append(TradeResult(entry_index, index, entry_price, candle.close, pnl_pct, 'time_exit'))
            position_entry = None
            continue

    return trades


def summarize(trades: list[TradeResult]) -> dict:
    if not trades:
        return {'trades': 0, 'win_rate': 0.0, 'avg_pnl_pct': 0.0, 'total_pnl_pct': 0.0, 'max_win_pct': 0.0, 'max_loss_pct': 0.0, 'score': -999.0}
    pnl_values = [trade.pnl_pct for trade in trades]
    wins = sum(1 for value in pnl_values if value > 0)
    win_rate = wins / len(trades) * 100
    total = sum(pnl_values) * 100
    max_loss = min(pnl_values) * 100
    score = total + win_rate * 0.03 + max_loss * 1.5
    return {
        'trades': len(trades),
        'win_rate': round(win_rate, 2),
        'avg_pnl_pct': round(sum(pnl_values) / len(trades) * 100, 4),
        'total_pnl_pct': round(total, 4),
        'max_win_pct': round(max(pnl_values) * 100, 4),
        'max_loss_pct': round(max_loss, 4),
        'score': round(score, 4),
    }


def parse_float_list(raw: str) -> list[float]:
    return [float(item.strip()) for item in raw.split(',') if item.strip()]


def parse_int_list(raw: str) -> list[int]:
    return [int(item.strip()) for item in raw.split(',') if item.strip()]


def run_search(candles: list[Candle], thresholds: list[float], take_profits: list[float], stop_losses: list[float], max_holds: list[int], top: int) -> list[dict]:
    results = []
    for threshold, take_profit, stop_loss, max_hold in itertools.product(thresholds, take_profits, stop_losses, max_holds):
        trades = run_backtest(candles, threshold, take_profit, stop_loss, max_hold)
        summary = summarize(trades)
        if summary['trades'] <= 0:
            continue
        results.append({
            'threshold': threshold,
            'take_profit': take_profit,
            'stop_loss': stop_loss,
            'max_hold_bars': max_hold,
            **summary,
        })
    results.sort(key=lambda item: (item['score'], item['total_pnl_pct'], item['win_rate'], item['trades']), reverse=True)
    return results[:top]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='DRAM threshold backtest')
    parser.add_argument('--csv', required=True, type=Path, help='historical candle CSV')
    parser.add_argument('--threshold', type=float, default=60.0, help='long entry threshold')
    parser.add_argument('--take-profit', dest='take_profit', type=float, default=0.03, help='take profit pct')
    parser.add_argument('--stop-loss', dest='stop_loss', type=float, default=0.12, help='stop loss pct')
    parser.add_argument('--max-hold-bars', type=int, default=24, help='max bars to hold')
    parser.add_argument('--search', action='store_true', help='run parameter grid search')
    parser.add_argument('--thresholds', default='58,59,60,61,62,63,64,65', help='comma-separated thresholds')
    parser.add_argument('--take-profits', default='0.01,0.015,0.02,0.03,0.04', help='comma-separated take profit pct values')
    parser.add_argument('--stop-losses', default='0.04,0.06,0.08,0.12', help='comma-separated stop loss pct values')
    parser.add_argument('--max-holds', default='12,24,48,72', help='comma-separated max hold bars')
    parser.add_argument('--top', type=int, default=12, help='top search rows to print')
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    candles = load_candles(args.csv)
    if len(candles) < 2:
        raise SystemExit('need at least 2 candles, got %d' % len(candles))
    if args.search:
        rows = run_search(
            candles,
            parse_float_list(args.thresholds),
            parse_float_list(args.take_profits),
            parse_float_list(args.stop_losses),
            parse_int_list(args.max_holds),
            args.top,
        )
        print('DRAM threshold parameter search')
        print('file=%s candles=%d' % (args.csv, len(candles)))
        for index, row in enumerate(rows, 1):
            print('%02d %s' % (index, row))
        return
    trades = run_backtest(candles, args.threshold, args.take_profit, args.stop_loss, args.max_hold_bars)
    summary = summarize(trades)
    print('DRAM threshold backtest')
    print('file=%s' % args.csv)
    print('threshold=%s take_profit=%s stop_loss=%s max_hold_bars=%s' % (args.threshold, args.take_profit, args.stop_loss, args.max_hold_bars))
    print(summary)
    for trade in trades[-10:]:
        print({
            'entry_index': trade.entry_index,
            'exit_index': trade.exit_index,
            'entry_price': round(trade.entry_price, 6),
            'exit_price': round(trade.exit_price, 6),
            'pnl_pct': round(trade.pnl_pct * 100, 4),
            'exit_reason': trade.exit_reason,
        })


if __name__ == '__main__':
    main()
