#!/usr/bin/env python3
"""Independent walk-forward research for an ATR-scaled inventory-aware grid."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


MAKER_FEE = 0.0002
TAKER_FEE = 0.0005
SLIPPAGE = 0.0002
INITIAL_EQUITY = 1000.0
LAYER_NOTIONAL = 10.0


def _load_candles(symbol: str, days: int, root: Path) -> pd.DataFrame:
    path = root / f"{symbol}_{days}d_1m.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    frame = frame.sort_values("ts").drop_duplicates("ts").set_index("ts")
    return frame[["open", "high", "low", "close", "volume"]]


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (
        frame.resample(rule, label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def _load_funding(symbol: str, days: int, root: Path) -> pd.DataFrame:
    path = root / f"{symbol}_funding_{days}d.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not rows:
        return pd.DataFrame(columns=["funding_rate"])
    frame = pd.DataFrame(rows)
    ts = frame["fundingTime"].astype("int64")
    rate = frame["realizedRate"].where(frame["realizedRate"].notna(), frame["fundingRate"])
    return (
        pd.DataFrame(
            {
                "ts": pd.to_datetime(ts, unit="ms", utc=True),
                "funding_rate": pd.to_numeric(rate, errors="coerce").fillna(0.0),
            }
        )
        .drop_duplicates("ts")
        .set_index("ts")
        .sort_index()[["funding_rate"]]
    )


def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    previous = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - previous).abs(),
            (out["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = true_range.rolling(14).mean()
    out["ema_slow"] = out["close"].ewm(span=100, adjust=False).mean()
    return out


def _max_drawdown(curve: list[float]) -> float:
    peak = curve[0] if curve else 0.0
    worst = 0.0
    for value in curve:
        peak = max(peak, value)
        worst = max(worst, peak - value)
    return worst


def _trade_metrics(
    trades: list[dict[str, Any]],
    equity_curve: list[float],
    funding_paid: float,
    fees_paid: float,
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
) -> dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "net_pnl": 0.0,
            "gross_pnl": 0.0,
            "fees": 0.0,
            "funding": 0.0,
            "profit_factor": 0.0,
            "win_rate_pct": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "recovery_days": 0.0,
            "avg_trade": 0.0,
            "avg_hold_hours": 0.0,
            "long_net": 0.0,
            "short_net": 0.0,
            "fee_to_gross_pct": 0.0,
            "daily_positive_pct": 0.0,
            "rolling_14d_positive_pct": 0.0,
            "exposure_pct": 0.0,
        }
    gross_profit = sum(max(0.0, float(t["gross_pnl"])) for t in trades)
    gross_loss = abs(sum(min(0.0, float(t["gross_pnl"])) for t in trades))
    net_values = [float(t["net_pnl"]) for t in trades]
    max_dd = _max_drawdown(equity_curve)
    peak = equity_curve[0]
    peak_index = 0
    trough_curve_index = 0
    worst = 0.0
    for index, value in enumerate(equity_curve):
        if value > peak:
            peak = value
            peak_index = index
        dd = peak - value
        if dd > worst:
            worst = dd
            trough_curve_index = index
    recovery_index = None
    recovery_peak = equity_curve[peak_index]
    for index in range(trough_curve_index + 1, len(equity_curve)):
        if equity_curve[index] >= recovery_peak:
            recovery_index = index
            break
    exit_series = pd.Series(
        net_values,
        index=pd.DatetimeIndex([t["exit_ts"] for t in trades]),
    )
    daily = exit_series.resample("1D").sum()
    rolling = daily.rolling("14D").sum().dropna()
    exposure_bars = sum(int(t["hold_bars"]) for t in trades)
    total_bars = max(1, int((end_ts - start_ts).total_seconds() / 300))
    return {
        "trades": len(trades),
        "net_pnl": sum(net_values),
        "gross_pnl": sum(float(t["gross_pnl"]) for t in trades),
        "fees": fees_paid,
        "funding": funding_paid,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        "win_rate_pct": sum(value > 0 for value in net_values) / len(net_values) * 100,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd / INITIAL_EQUITY * 100,
        "recovery_days": (
            (
                end_ts
                - trades[
                    min(
                        len(trades) - 1,
                        int(
                            trough_curve_index
                            / max(1, len(equity_curve) - 1)
                            * max(0, len(trades) - 1)
                        ),
                    )
                ]["exit_ts"]
            ).total_seconds()
            / 86400
            if recovery_index is None
            else (
                trades[min(len(trades) - 1, recovery_index)]["exit_ts"]
                - trades[
                    min(
                        len(trades) - 1,
                        int(
                            trough_curve_index
                            / max(1, len(equity_curve) - 1)
                            * max(0, len(trades) - 1)
                        ),
                    )
                ]["exit_ts"]
            ).total_seconds()
            / 86400
        ),
        "avg_trade": statistics.fmean(net_values),
        "avg_hold_hours": statistics.fmean(float(t["hold_hours"]) for t in trades),
        "long_net": sum(float(t["net_pnl"]) for t in trades if t["side"] == "long"),
        "short_net": sum(float(t["net_pnl"]) for t in trades if t["side"] == "short"),
        "fee_to_gross_pct": fees_paid / gross_profit * 100 if gross_profit > 0 else 999.0,
        "daily_positive_pct": (daily > 0).mean() * 100 if len(daily) else 0.0,
        "rolling_14d_positive_pct": (rolling > 0).mean() * 100 if len(rolling) else 0.0,
        "exposure_pct": exposure_bars / total_bars * 100,
    }


def simulate_grid(
    frame: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    atr_mult: float,
    tp_mult: float,
    max_layers: int,
    trend_filter: bool,
    stop_atr: float,
    max_hold_bars: int,
    cooldown_bars: int,
    min_step: float = 0.0015,
    max_step: float = 0.02,
) -> dict[str, Any]:
    data = _indicators(frame)
    arrays = {name: data[name].to_numpy() for name in data.columns}
    timestamps = data.index
    funding_events = funding.reindex(data.index).fillna(0.0)["funding_rate"].to_numpy()
    long_lots: list[dict[str, Any]] = []
    short_lots: list[dict[str, Any]] = []
    active_long_levels: set[int] = set()
    active_short_levels: set[int] = set()
    long_block_until = 0
    short_block_until = 0
    anchor = float(arrays["close"][0])
    cash = 0.0
    fees_paid = 0.0
    funding_paid = 0.0
    trades: list[dict[str, Any]] = []
    equity_curve = [INITIAL_EQUITY]

    def close_lot(lot: dict[str, Any], price: float, ts: pd.Timestamp, reason: str) -> None:
        nonlocal cash, fees_paid
        if lot["side"] == "long":
            gross = lot["notional"] * (price / lot["entry"] - 1.0)
        else:
            gross = lot["notional"] * (1.0 - price / lot["entry"])
        fee_rate = MAKER_FEE if reason in {"target"} else TAKER_FEE + SLIPPAGE
        fee = lot["notional"] * fee_rate
        cash += gross - fee
        fees_paid += fee
        trades.append(
            {
                "side": lot["side"],
                "entry_ts": lot["entry_ts"],
                "exit_ts": ts,
                "entry": lot["entry"],
                "exit": price,
                "notional": lot["notional"],
                "gross_pnl": gross,
                "fees": fee,
                "funding": float(lot.get("funding", 0.0)),
                "net_pnl": gross - fee + float(lot.get("funding", 0.0)),
                "hold_bars": max(0, int((ts - lot["entry_ts"]).total_seconds() / 300)),
                "hold_hours": max(0.0, (ts - lot["entry_ts"]).total_seconds() / 3600),
                "exit_reason": reason,
            }
        )

    def side_average(lots: list[dict[str, Any]]) -> float:
        total = sum(float(lot["notional"]) for lot in lots)
        if total <= 0:
            return 0.0
        return sum(float(lot["entry"]) * float(lot["notional"]) for lot in lots) / total

    for i in range(1, len(data)):
        ts = timestamps[i]
        atr = float(arrays["atr"][i - 1])
        close = float(arrays["close"][i])
        high = float(arrays["high"][i])
        low = float(arrays["low"][i])
        if close <= 0 or not np.isfinite(atr):
            equity_curve.append(INITIAL_EQUITY + cash)
            continue
        previous_close = float(arrays["close"][i - 1])
        atr_ratio = atr / previous_close
        step = min(max_step, max(min_step, atr_ratio * atr_mult))
        tp = step * tp_mult
        ema_slow = float(arrays["ema_slow"][i - 1])
        long_allowed = not trend_filter or previous_close >= ema_slow
        short_allowed = not trend_filter or previous_close <= ema_slow

        # Accrue funding against each open lot and realise it when the lot closes.
        rate = float(funding_events[i])
        if rate:
            long_notional = sum(float(lot["notional"]) for lot in long_lots)
            short_notional = sum(float(lot["notional"]) for lot in short_lots)
            cost = rate * (long_notional - short_notional)
            funding_paid += cost
            for lot in long_lots:
                lot["funding"] = float(lot.get("funding", 0.0)) - rate * float(lot["notional"])
            for lot in short_lots:
                lot["funding"] = float(lot.get("funding", 0.0)) + rate * float(lot["notional"])

        # TP exits use limit prices and are evaluated before new entries.
        surviving_long = []
        for lot in long_lots:
            if high >= lot["target"] and ts > lot["entry_ts"]:
                close_lot(lot, lot["target"], ts, "target")
                active_long_levels.discard(lot["level"])
            else:
                surviving_long.append(lot)
        long_lots = surviving_long
        surviving_short = []
        for lot in short_lots:
            if low <= lot["target"] and ts > lot["entry_ts"]:
                close_lot(lot, lot["target"], ts, "target")
                active_short_levels.discard(lot["level"])
            else:
                surviving_short.append(lot)
        short_lots = surviving_short

        # Inventory stops and time stops execute at adverse levels with taker costs.
        if long_lots and stop_atr > 0:
            average = side_average(long_lots)
            stop_px = average * (1.0 - stop_atr * atr_ratio)
            if low <= stop_px:
                execution = min(float(arrays["open"][i]), stop_px)
                for lot in list(long_lots):
                    close_lot(lot, execution, ts, "inventory_stop")
                long_lots.clear()
                active_long_levels.clear()
                long_block_until = i + cooldown_bars
        if short_lots and stop_atr > 0:
            average = side_average(short_lots)
            stop_px = average * (1.0 + stop_atr * atr_ratio)
            if high >= stop_px:
                execution = max(float(arrays["open"][i]), stop_px)
                for lot in list(short_lots):
                    close_lot(lot, execution, ts, "inventory_stop")
                short_lots.clear()
                active_short_levels.clear()
                short_block_until = i + cooldown_bars
        if long_lots and i - int(long_lots[0]["entry_i"]) >= max_hold_bars:
            for lot in list(long_lots):
                close_lot(lot, close, ts, "time_stop")
            long_lots.clear()
            active_long_levels.clear()
            long_block_until = i + cooldown_bars
        if short_lots and i - int(short_lots[0]["entry_i"]) >= max_hold_bars:
            for lot in list(short_lots):
                close_lot(lot, close, ts, "time_stop")
            short_lots.clear()
            active_short_levels.clear()
            short_block_until = i + cooldown_bars

        if long_allowed and i >= long_block_until and len(long_lots) < max_layers:
            for level in range(1, max_layers + 1):
                if level in active_long_levels:
                    continue
                limit = anchor * (1.0 - level * step)
                if low <= limit:
                    long_lots.append(
                        {
                            "side": "long",
                            "entry": limit,
                            "target": limit * (1.0 + tp),
                            "notional": LAYER_NOTIONAL,
                            "entry_ts": ts,
                            "entry_i": i,
                            "level": level,
                            "funding": 0.0,
                        }
                    )
                    active_long_levels.add(level)
                    fees_paid += LAYER_NOTIONAL * MAKER_FEE
                    cash -= LAYER_NOTIONAL * MAKER_FEE
                    break
        if short_allowed and i >= short_block_until and len(short_lots) < max_layers:
            for level in range(1, max_layers + 1):
                if level in active_short_levels:
                    continue
                limit = anchor * (1.0 + level * step)
                if high >= limit:
                    short_lots.append(
                        {
                            "side": "short",
                            "entry": limit,
                            "target": limit * (1.0 - tp),
                            "notional": LAYER_NOTIONAL,
                            "entry_ts": ts,
                            "entry_i": i,
                            "level": level,
                            "funding": 0.0,
                        }
                    )
                    active_short_levels.add(level)
                    fees_paid += LAYER_NOTIONAL * MAKER_FEE
                    cash -= LAYER_NOTIONAL * MAKER_FEE
                    break
        drift = abs(close - anchor) / anchor
        if drift >= max(step, 0.006):
            anchor = close

        open_pnl = 0.0
        for lot in long_lots:
            open_pnl += lot["notional"] * (close / lot["entry"] - 1.0)
        for lot in short_lots:
            open_pnl += lot["notional"] * (1.0 - close / lot["entry"])
        equity_curve.append(
            INITIAL_EQUITY
            + cash
            + open_pnl
            + sum(float(lot.get("funding", 0.0)) for lot in long_lots + short_lots)
        )

    # Close remaining inventory at the final close so results are not left open.
    last_index = len(data) - 1
    final_ts = timestamps[last_index]
    final_close = float(arrays["close"][last_index])
    for lot in list(long_lots) + list(short_lots):
        close_lot(lot, final_close, final_ts, "end")
    equity_curve.append(INITIAL_EQUITY + cash)
    metrics = _trade_metrics(
        trades,
        equity_curve,
        funding_paid,
        fees_paid,
        timestamps[0],
        timestamps[-1],
    )
    return {
        "strategy": "volatility_grid",
        "metrics": metrics,
        "trades": trades,
    }


def _parameter_sets() -> list[dict[str, Any]]:
    return [
        {
            "atr_mult": atr_mult,
            "tp_mult": tp_mult,
            "max_layers": layers,
            "trend_filter": trend_filter,
            "stop_atr": stop_atr,
            "max_hold_bars": hold,
            "cooldown_bars": cooldown,
        }
        for atr_mult, tp_mult, layers, trend_filter, stop_atr, hold, cooldown in itertools.product(
            (0.6, 0.9),
            (1.75, 2.5),
            (2, 3),
            (False, True),
            (0.0, 2.0),
            (288,),
            (24,),
        )
    ]


def _rank(train_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in train_rows
        if row["metrics"]["net_pnl"] > 0 and row["metrics"]["trades"] >= 8
    ]
    if not eligible:
        return []
    return sorted(
        eligible,
        key=lambda row: (
            row["metrics"]["rolling_14d_positive_pct"],
            row["metrics"]["net_pnl"] / max(1.0, row["metrics"]["max_drawdown"]),
            row["metrics"]["profit_factor"],
        ),
        reverse=True,
    )


def walk_forward(
    symbol: str,
    frame: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    train_days: int,
    test_days: int,
) -> dict[str, Any]:
    bars_per_day = 24 * 12
    train_bars = train_days * bars_per_day
    test_bars = test_days * bars_per_day
    folds = []
    for start in range(0, len(frame) - train_bars - test_bars + 1, test_bars):
        train = frame.iloc[start : start + train_bars]
        test = frame.iloc[start + train_bars : start + train_bars + test_bars]
        train_funding = funding.loc[
            (funding.index >= train.index[0]) & (funding.index <= train.index[-1])
        ]
        test_funding = funding.loc[
            (funding.index >= test.index[0]) & (funding.index <= test.index[-1])
        ]
        rows = [
            {"params": params, "metrics": simulate_grid(train, train_funding, **params)["metrics"]}
            for params in _parameter_sets()
        ]
        ranked = _rank(rows)
        if not ranked:
            folds.append(
                {
                    "start": train.index[0].isoformat(),
                    "test_start": test.index[0].isoformat(),
                    "test_end": test.index[-1].isoformat(),
                    "selected_params": None,
                    "train_metrics": None,
                    "test_metrics": _trade_metrics(
                        [], [INITIAL_EQUITY], 0.0, 0.0, test.index[0], test.index[-1]
                    ),
                    "test_trades": [],
                    "selection_failed": True,
                }
            )
            continue
        selected = ranked[0]
        result = simulate_grid(test, test_funding, **selected["params"])
        test_candidates = []
        for candidate in ranked[:5]:
            candidate_result = simulate_grid(test, test_funding, **candidate["params"])
            test_candidates.append(
                {
                    "params": candidate["params"],
                    "train_metrics": candidate["metrics"],
                    "test_metrics": candidate_result["metrics"],
                }
            )
        folds.append(
            {
                "start": train.index[0].isoformat(),
                "test_start": test.index[0].isoformat(),
                "test_end": test.index[-1].isoformat(),
                "selected_params": selected["params"],
                "train_metrics": selected["metrics"],
                "test_metrics": result["metrics"],
                "test_candidates": test_candidates,
                "test_trades": [
                    {
                        **trade,
                        "entry_ts": trade["entry_ts"].isoformat(),
                        "exit_ts": trade["exit_ts"].isoformat(),
                    }
                    for trade in result["trades"]
                ],
                "selection_failed": False,
            }
        )
    trades = []
    for fold in folds:
        for trade in fold["test_trades"]:
            item = dict(trade)
            item["entry_ts"] = pd.Timestamp(item["entry_ts"])
            item["exit_ts"] = pd.Timestamp(item["exit_ts"])
            trades.append(item)
    curve = [INITIAL_EQUITY]
    running = INITIAL_EQUITY
    for trade in trades:
        running += float(trade["net_pnl"])
        curve.append(running)
    oos_metrics = _trade_metrics(
        trades,
        curve,
        sum(float(trade.get("funding", 0.0)) for trade in trades),
        sum(float(trade["fees"]) for trade in trades),
        min(frame.index[0], trades[0]["entry_ts"]) if trades else frame.index[0],
        max(frame.index[-1], trades[-1]["exit_ts"]) if trades else frame.index[-1],
    )
    return {
        "symbol": symbol,
        "strategy": "volatility_grid",
        "train_days": train_days,
        "test_days": test_days,
        "folds": folds,
        "oos_metrics": oos_metrics,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default="XAU-USDT-SWAP,ETH-USDT-SWAP,CRV-USDT-SWAP,FARTCOIN-USDT-SWAP,DRAM-USDT-SWAP",
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/research"))
    parser.add_argument("--out", type=Path, default=Path("data/research/volatility_grid_report.json"))
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = []
    for symbol in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        one_minute = _load_candles(symbol, args.days, args.cache_dir)
        five_minute = _resample(one_minute, "5min")
        funding = _load_funding(symbol, args.days, args.cache_dir)
        result = walk_forward(
            symbol,
            five_minute,
            funding,
            train_days=args.train_days,
            test_days=args.test_days,
        )
        report.append(result)
        metrics = result["oos_metrics"]
        print(
            f"{symbol} volatility_grid: trades={metrics['trades']} net={metrics['net_pnl']:.4f} "
            f"pf={metrics['profit_factor']:.3f} dd={metrics['max_drawdown']:.4f} "
            f"rolling14={metrics['rolling_14d_positive_pct']:.1f}%"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
