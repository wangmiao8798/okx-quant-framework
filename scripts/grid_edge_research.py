#!/usr/bin/env python3
"""Read-only grid parameter research for the live futures hedge strategy.

The simulator is intentionally separate from the production strategy:
- no credentials
- no order submission
- no imports from the live execution path

It fetches OKX public candles, replays a conservative one-minute grid model,
and writes a JSON report. The model forbids same-candle entry and exit, which
avoids the most common optimistic bias in candle-based grid backtests.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


FEE_MAKER = 0.0002
FEE_TAKER = 0.0005
T0_FEE = 0.0002
FUNDING_RATE_8H = 0.0001
BAR_MS = 60_000


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Lot:
    side: str
    entry: float
    tp: float
    notional: float
    opened_ts: int
    level: int


@dataclass
class Result:
    symbol: str
    step_ratio: float
    tp_ratio: float
    max_layers: int
    side_mode: str
    drift_threshold: float
    candles: int
    start: str
    end: str
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_pnl: float
    fees: float
    funding: float
    net_pnl: float
    long_net_pnl: float
    short_net_pnl: float
    avg_net_per_trade: float
    avg_hold_minutes: float
    max_open_notional: float
    max_net_notional: float
    max_drawdown: float
    return_on_peak_margin_pct: float
    fills: int
    ending_unrealized: float
    ending_open_notional: float
    score: float


def _fetch_candles(symbol: str, days: int, cache_dir: Path) -> list[Candle]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}_{days}d_1m.json"
    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        candles = [Candle(**row) for row in payload]
        if (
            len(candles) >= int(days * 24 * 60 * 0.8)
            and candles
            and time.time() * 1000 - candles[-1].ts < 10 * 60_000
        ):
            return candles

    from okx.MarketData import MarketAPI

    api = MarketAPI("", "", "", flag="0")
    target = days * 24 * 60
    after = str(int(time.time() * 1000))
    rows: list[Candle] = []
    while len(rows) < target:
        kwargs = {
            "instId": symbol,
            "bar": "1m",
            "limit": "300",
            "after": after,
        }
        response = api.get_history_candlesticks(**kwargs)
        if str(response.get("code", "")) != "0":
            raise RuntimeError(f"{symbol} candle fetch failed: {response}")
        batch = response.get("data") or []
        if not batch:
            break
        for item in batch:
            rows.append(
                Candle(
                    ts=int(item[0]),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                )
            )
        after = str(batch[-1][0])
        time.sleep(0.12)

    deduped = {row.ts: row for row in rows}
    candles = [deduped[ts] for ts in sorted(deduped)]
    cache_path.write_text(
        json.dumps([asdict(row) for row in candles], separators=(",", ":")),
        encoding="utf-8",
    )
    return candles


def _mark_to_market(lots: Iterable[Lot], price: float) -> float:
    total = 0.0
    for lot in lots:
        if lot.side == "long":
            total += lot.notional * (price / lot.entry - 1.0)
        else:
            total += lot.notional * (1.0 - price / lot.entry)
    return total


def _max_drawdown(values: list[float]) -> float:
    peak = 0.0
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def _sharpe(values: list[float], periods_per_day: int = 1440) -> float:
    if len(values) < 2:
        return 0.0
    returns = []
    previous = values[0]
    for value in values[1:]:
        delta = value - previous
        returns.append(delta / 50.0)
        previous = value
    if len(returns) < 2:
        return 0.0
    avg = statistics.fmean(returns)
    stdev = statistics.stdev(returns)
    if stdev <= 1e-12:
        return 0.0
    return avg / stdev * math.sqrt(periods_per_day * 365)


def _recenter(
    ts: int,
    side: str,
    anchor: float,
    step_ratio: float,
    max_layers: int,
    active_levels: set[int],
) -> float:
    return anchor


def _can_enter(active_levels: set[int], level: int, max_layers: int) -> bool:
    return 0 <= level <= max_layers and level not in active_levels


def simulate(
    candles: list[Candle],
    *,
    symbol: str,
    step_ratio: float,
    tp_ratio: float,
    max_layers: int,
    side_mode: str,
    layer_notional: float,
    drift_threshold: float,
    take_profit_multiplier: float,
    trend_minutes: int = 0,
    base_mode: str = "both",
    tp_age_minutes: int = 0,
    tp_age_scale: float = 1.0,
) -> Result:
    if not candles:
        raise ValueError("candles are required")

    first = candles[0]
    anchor = first.close
    long_lots: list[Lot] = []
    short_lots: list[Lot] = []
    long_levels: set[int] = set()
    short_levels: set[int] = set()
    closed: list[dict] = []
    realized = 0.0
    fees_paid = 0.0
    funding_paid = 0.0
    fills = 0
    max_open_notional = 0.0
    max_net_notional = 0.0
    equity_curve: list[float] = []
    rolling_closes = [first.close]
    rolling_sum = first.close

    def add_lot(side: str, price: float, level: int, ts: int) -> None:
        nonlocal fills, fees_paid
        if side == "long":
            target = price * (1.0 + tp_ratio * take_profit_multiplier)
            long_lots.append(Lot(side, price, target, layer_notional, ts, level))
            long_levels.add(level)
        else:
            target = price * (1.0 - tp_ratio * take_profit_multiplier)
            short_lots.append(Lot(side, price, target, layer_notional, ts, level))
            short_levels.add(level)
        fee = layer_notional * FEE_MAKER
        fees_paid += fee
        fills += 1

    if base_mode == "both" and side_mode in {"both", "long"}:
        long_lots.append(
            Lot("long", first.close, first.close * (1.0 + tp_ratio), layer_notional, first.ts, -1)
        )
        fees_paid += layer_notional * FEE_TAKER
    if base_mode == "both" and side_mode in {"both", "short"}:
        short_lots.append(
            Lot("short", first.close, first.close * (1.0 - tp_ratio), layer_notional, first.ts, -1)
        )
        fees_paid += layer_notional * FEE_TAKER

    previous_close = first.close
    for candle in candles[1:]:
        trend_long_allowed = True
        trend_short_allowed = True
        if trend_minutes > 0 and len(rolling_closes) >= trend_minutes:
            moving_average = rolling_sum / trend_minutes
            trend_long_allowed = previous_close >= moving_average
            trend_short_allowed = previous_close <= moving_average

        hour_utc = datetime.fromtimestamp(candle.ts / 1000, tz=timezone.utc).hour
        if hour_utc in {0, 8, 16} and candle.ts % (60 * 60 * 1000) == 0:
            net_units = sum(lot.notional for lot in long_lots) - sum(
                lot.notional for lot in short_lots
            )
            funding = abs(net_units) * FUNDING_RATE_8H
            funding_paid += funding
            realized -= funding

        # Close existing lots before opening new ones. This prevents a candle's
        # high/low from creating a same-candle entry and exit.
        for side, lots, levels in (
            ("long", long_lots, long_levels),
            ("short", short_lots, short_levels),
        ):
            surviving: list[Lot] = []
            for lot in lots:
                age_minutes = (candle.ts - lot.opened_ts) / 60_000
                target_tp = lot.tp
                if tp_age_minutes > 0 and age_minutes >= tp_age_minutes:
                    ratio = max(FEE_MAKER * 2 + 0.0005, tp_ratio * tp_age_scale)
                    target_tp = (
                        lot.entry * (1.0 + ratio)
                        if side == "long"
                        else lot.entry * (1.0 - ratio)
                    )
                hit = candle.high >= target_tp if side == "long" else candle.low <= target_tp
                if hit and candle.ts > lot.opened_ts:
                    gross = (
                        lot.notional * (target_tp / lot.entry - 1.0)
                        if side == "long"
                        else lot.notional * (1.0 - target_tp / lot.entry)
                    )
                    fee = lot.notional * FEE_MAKER
                    net = gross - fee
                    realized += net
                    fees_paid += fee
                    closed.append(
                        {
                            "side": side,
                            "net": net,
                            "gross": gross,
                            "fee": fee,
                            "opened_ts": lot.opened_ts,
                            "closed_ts": candle.ts,
                            "level": lot.level,
                        }
                    )
                    if lot.level >= 0:
                        levels.discard(lot.level)
                else:
                    surviving.append(lot)
            if side == "long":
                long_lots = surviving
            else:
                short_lots = surviving

        # One grid attempt per side per minute. Re-entry is allowed after TP.
        if (
            side_mode in {"both", "long"}
            and trend_long_allowed
            and len(long_lots) < max_layers + 1
        ):
            for level in range(1, max_layers + 1):
                if not _can_enter(long_levels, level, max_layers):
                    continue
                limit = anchor * (1.0 - level * step_ratio)
                if candle.low <= limit:
                    add_lot("long", limit, level, candle.ts)
                    break

        if (
            side_mode in {"both", "short"}
            and trend_short_allowed
            and len(short_lots) < max_layers + 1
        ):
            for level in range(1, max_layers + 1):
                if not _can_enter(short_levels, level, max_layers):
                    continue
                limit = anchor * (1.0 + level * step_ratio)
                if candle.high >= limit:
                    add_lot("short", limit, level, candle.ts)
                    break

        mark = realized + _mark_to_market(long_lots + short_lots, candle.close)
        equity_curve.append(mark)
        max_open_notional = max(
            max_open_notional,
            sum(lot.notional for lot in long_lots) + sum(lot.notional for lot in short_lots),
        )
        max_net_notional = max(
            max_net_notional,
            abs(sum(lot.notional for lot in long_lots) - sum(lot.notional for lot in short_lots)),
        )

        drift = abs(candle.close - anchor) / anchor if anchor > 0 else 0.0
        if drift >= drift_threshold:
            anchor = candle.close
        previous_close = candle.close
        rolling_closes.append(candle.close)
        rolling_sum += candle.close
        if trend_minutes > 0 and len(rolling_closes) > trend_minutes:
            rolling_sum -= rolling_closes[-trend_minutes - 1]
            rolling_closes = rolling_closes[-trend_minutes:]

    ending_price = candles[-1].close
    ending_unrealized = _mark_to_market(long_lots + short_lots, ending_price)
    ending_open_notional = sum(lot.notional for lot in long_lots) + sum(
        lot.notional for lot in short_lots
    )
    total_net = realized + ending_unrealized
    wins = sum(1 for trade in closed if trade["net"] > 0)
    losses = sum(1 for trade in closed if trade["net"] < 0)
    long_net = sum(trade["net"] for trade in closed if trade["side"] == "long")
    short_net = sum(trade["net"] for trade in closed if trade["side"] == "short")
    avg_hold = (
        statistics.fmean((trade["closed_ts"] - trade["opened_ts"]) / 60_000 for trade in closed)
        if closed
        else 0.0
    )
    peak_margin = max(1.0, max_open_notional / 3.0)
    max_dd = _max_drawdown(equity_curve)
    score = (total_net / max(1.0, max_dd)) * math.log1p(len(closed))
    return Result(
        symbol=symbol,
        step_ratio=step_ratio,
        tp_ratio=tp_ratio,
        max_layers=max_layers,
        side_mode=side_mode,
        drift_threshold=drift_threshold,
        candles=len(candles),
        start=datetime.fromtimestamp(candles[0].ts / 1000).isoformat(),
        end=datetime.fromtimestamp(candles[-1].ts / 1000).isoformat(),
        trades=len(closed),
        wins=wins,
        losses=losses,
        win_rate_pct=(wins / len(closed) * 100.0) if closed else 0.0,
        gross_pnl=sum(float(trade["gross"]) for trade in closed),
        fees=fees_paid,
        funding=funding_paid,
        net_pnl=total_net,
        long_net_pnl=long_net,
        short_net_pnl=short_net,
        avg_net_per_trade=(sum(trade["net"] for trade in closed) / len(closed)) if closed else 0.0,
        avg_hold_minutes=avg_hold,
        max_open_notional=max_open_notional,
        max_net_notional=max_net_notional,
        max_drawdown=max_dd,
        return_on_peak_margin_pct=total_net / peak_margin * 100.0,
        fills=fills,
        ending_unrealized=ending_unrealized,
        ending_open_notional=ending_open_notional,
        score=score,
    )


def _write_csv_report(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _split(candles: list[Candle], train_ratio: float) -> tuple[list[Candle], list[Candle]]:
    split = max(1, min(len(candles) - 1, int(len(candles) * train_ratio)))
    return candles[:split], candles[split:]


def run_research(
    candles: list[Candle],
    *,
    symbol: str,
    steps: list[float],
    tps: list[float],
    layers: list[int],
    side_modes: list[str],
    drift_thresholds: list[float],
    trend_minutes: list[int],
    layer_notional: float,
    train_ratio: float,
) -> dict:
    train, test = _split(candles, train_ratio)
    rows: list[dict] = []
    for step in steps:
        for tp in tps:
            for max_layers in layers:
                for side_mode in side_modes:
                    for drift in drift_thresholds:
                        for trend in trend_minutes:
                            params = {
                                "symbol": symbol,
                                "step_ratio": step,
                                "tp_ratio": tp,
                                "max_layers": max_layers,
                                "side_mode": side_mode,
                                "drift_threshold": drift,
                                "trend_minutes": trend,
                            }
                            train_result = simulate(
                                train,
                                layer_notional=layer_notional,
                                take_profit_multiplier=1.0,
                                **params,
                            )
                            test_result = simulate(
                                test,
                                layer_notional=layer_notional,
                                take_profit_multiplier=1.0,
                                **params,
                            )
                            rows.append(
                                {
                                    **params,
                                    "train_net": train_result.net_pnl,
                                    "test_net": test_result.net_pnl,
                                    "train_trades": train_result.trades,
                                    "test_trades": test_result.trades,
                                    "train_dd": train_result.max_drawdown,
                                    "test_dd": test_result.max_drawdown,
                                    "train_return_on_margin_pct": train_result.return_on_peak_margin_pct,
                                    "test_return_on_margin_pct": test_result.return_on_peak_margin_pct,
                                    "robust_net": min(train_result.net_pnl, test_result.net_pnl),
                                    "robust_score": min(train_result.net_pnl, test_result.net_pnl)
                                    / max(1.0, max(train_result.max_drawdown, test_result.max_drawdown)),
                                    "full_net": simulate(
                                        candles,
                                        layer_notional=layer_notional,
                                        take_profit_multiplier=1.0,
                                        **params,
                                    ).net_pnl,
                                }
                            )
    ranked = sorted(
        rows,
        key=lambda row: (
            row["robust_net"] > 0,
            row["robust_score"],
            row["test_net"],
            row["full_net"],
        ),
        reverse=True,
    )
    return {
        "symbol": symbol,
        "train_ratio": train_ratio,
        "layer_notional_usdt": layer_notional,
        "parameters": {
            "steps": steps,
            "tps": tps,
            "layers": layers,
            "side_modes": side_modes,
            "drift_thresholds": drift_thresholds,
            "trend_minutes": trend_minutes,
        },
        "top": ranked[:20],
        "all_rows": len(rows),
    }


def _parse_floats(raw: str) -> list[float]:
    return [float(value.strip()) for value in raw.split(",") if value.strip()]


def _parse_ints(raw: str) -> list[int]:
    return [int(value.strip()) for value in raw.split(",") if value.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="XAU-USDT-SWAP,ETH-USDT-SWAP")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/research"))
    parser.add_argument("--out", type=Path, default=Path("data/research/grid_edge_report.json"))
    parser.add_argument("--steps", default="0.0008,0.0012,0.0018,0.0025,0.0035")
    parser.add_argument("--tps", default="0.0025,0.0035,0.005,0.007")
    parser.add_argument("--layers", default="1,2,3")
    parser.add_argument("--side-modes", default="both,long,short")
    parser.add_argument("--drifts", default="0.006,0.01")
    parser.add_argument("--trend-minutes", default="0")
    parser.add_argument("--layer-notional", type=float, default=10.0)
    parser.add_argument("--train-ratio", type=float, default=0.7)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reports = []
    for symbol in [value.strip() for value in args.symbols.split(",") if value.strip()]:
        candles = _fetch_candles(symbol, args.days, args.cache_dir)
        report = run_research(
            candles,
            symbol=symbol,
            steps=_parse_floats(args.steps),
            tps=_parse_floats(args.tps),
            layers=_parse_ints(args.layers),
            side_modes=[value.strip() for value in args.side_modes.split(",") if value.strip()],
            drift_thresholds=_parse_floats(args.drifts),
            trend_minutes=_parse_ints(args.trend_minutes),
            layer_notional=args.layer_notional,
            train_ratio=args.train_ratio,
        )
        reports.append(report)
        print(
            f"{symbol}: candles={len(candles)} parameter_sets={report['all_rows']} "
            f"top_robust_net={report['top'][0]['robust_net']:.4f}"
        )
        for row in report["top"][:5]:
            print(
                "  "
                f"step={row['step_ratio']:.4%} tp={row['tp_ratio']:.4%} "
                f"layers={row['max_layers']} side={row['side_mode']} "
                f"drift={row['drift_threshold']:.3%} trend={row['trend_minutes']}m "
                f"train={row['train_net']:.4f} test={row['test_net']:.4f} "
                f"dd={max(row['train_dd'], row['test_dd']):.4f}"
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    _write_csv_report(
        args.out.with_suffix(".csv"),
        [row for report in reports for row in report["top"]],
    )
    print(f"report={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
