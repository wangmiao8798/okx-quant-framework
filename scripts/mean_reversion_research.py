#!/usr/bin/env python3
"""Walk-forward research for short-horizon mean reversion."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, "/opt/okx_quant/scripts")

from strategy_family_research import (  # noqa: E402
    INITIAL_EQUITY,
    MAKER_FEE,
    RISK_PER_TRADE,
    SLIPPAGE,
    TAKER_FEE,
    _funding_pnl,
    _load_candles,
    _metrics_from_trades,
    _resample,
    _fetch_funding,
)


MAX_NOTIONAL = 100.0


def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    close = data["close"]
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    data["bb_z"] = (close - sma20) / std20.replace(0, np.nan)
    high20 = data["high"].rolling(20).max()
    low20 = data["low"].rolling(20).min()
    data["range_pos"] = (close - low20) / (high20 - low20).replace(0, np.nan) - 0.5
    previous = close.shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous).abs(),
            (data["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    data["atr14"] = true_range.rolling(14).mean()
    data["atr_pct"] = data["atr14"] / close
    data["vol_median"] = data["atr_pct"].rolling(288).median()
    return data


def run_mean_reversion(
    frame: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    feature: str,
    threshold: float,
    hold_bars: int,
    stop_atr: float,
    volatility_filter: bool,
    entry_offset: float = 0.0,
    target_pct: float = 0.0,
    risk_usdt: float = RISK_PER_TRADE,
) -> dict[str, Any]:
    data = _prepare(frame)
    arrays = {name: data[name].to_numpy() for name in data.columns}
    timestamps = data.index
    position = None
    trades: list[dict[str, Any]] = []
    for i in range(1, len(data)):
        low = float(arrays["low"][i])
        high = float(arrays["high"][i])
        close = float(arrays["close"][i])
        if position is None:
            signal = i - 1
            raw_signal = float(arrays[feature][signal])
            atr = float(arrays["atr14"][signal])
            atr_pct = float(arrays["atr_pct"][signal])
            vol_median = float(arrays["vol_median"][signal])
            if (
                not np.isfinite(raw_signal)
                or not np.isfinite(atr)
                or atr <= 0
                or (volatility_filter and not (np.isfinite(vol_median) and atr_pct >= vol_median))
            ):
                continue
            if raw_signal <= -threshold:
                side = "long"
            elif raw_signal >= threshold:
                side = "short"
            else:
                continue
            open_price = float(arrays["open"][i])
            raw_entry = (
                open_price * (1.0 - entry_offset)
                if side == "long"
                else open_price * (1.0 + entry_offset)
            )
            if entry_offset > 0:
                filled = low <= raw_entry if side == "long" else high >= raw_entry
                if not filled:
                    continue
            stop_distance = stop_atr * atr if stop_atr > 0 else 0.0
            if stop_distance > 0:
                notional = min(MAX_NOTIONAL, risk_usdt / max(stop_distance / raw_entry, 1e-6))
            else:
                notional = 50.0
            stop = (
                raw_entry - stop_distance
                if side == "long"
                else raw_entry + stop_distance
            ) if stop_distance > 0 else 0.0
            position = {
                "side": side,
                "entry_i": i,
                "entry_ts": timestamps[i],
                "entry_price": raw_entry,
                "notional": notional,
                "stop": stop,
                "min_price": raw_entry,
                "max_price": raw_entry,
            }
            continue
        position["min_price"] = min(position["min_price"], low)
        position["max_price"] = max(position["max_price"], high)
        exit_reason = ""
        raw_exit = 0.0
        target = (
            position["entry_price"] * (1.0 + target_pct)
            if position["side"] == "long"
            else position["entry_price"] * (1.0 - target_pct)
        )
        if target_pct > 0 and (
            (position["side"] == "long" and high >= target)
            or (position["side"] == "short" and low <= target)
        ):
            raw_exit = target
            exit_reason = "target"
        elif position["side"] == "long" and position["stop"] > 0 and low <= position["stop"]:
            raw_exit = min(float(arrays["open"][i]), position["stop"])
            exit_reason = "stop"
        elif position["side"] == "short" and position["stop"] > 0 and high >= position["stop"]:
            raw_exit = max(float(arrays["open"][i]), position["stop"])
            exit_reason = "stop"
        elif i - position["entry_i"] >= hold_bars:
            raw_exit = close
            exit_reason = "time"
        if not exit_reason:
            continue
        entry_cost = MAKER_FEE if entry_offset > 0 else TAKER_FEE + SLIPPAGE
        exit_cost = MAKER_FEE if exit_reason == "target" else TAKER_FEE + SLIPPAGE
        cost = entry_cost + exit_cost
        gross = (
            position["notional"] * (raw_exit / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - raw_exit / position["entry_price"])
        )
        fees = position["notional"] * cost
        funding_pnl, funding_events = _funding_pnl(
            funding,
            position["entry_ts"],
            timestamps[i],
            position["side"],
            position["notional"],
        )
        worst_price = (
            position["min_price"] if position["side"] == "long" else position["max_price"]
        )
        mae = (
            position["notional"] * (worst_price / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - worst_price / position["entry_price"])
        )
        trades.append(
            {
                "side": position["side"],
                "entry_ts": position["entry_ts"],
                "exit_ts": timestamps[i],
                "gross_pnl": gross,
                "fees": fees,
                "funding": funding_pnl,
                "funding_events": funding_events,
                "net_pnl": gross - fees + funding_pnl,
                "mae": min(0.0, mae - fees + funding_pnl),
                "hold_bars": i - position["entry_i"],
                "hold_hours": (timestamps[i] - position["entry_ts"]).total_seconds() / 3600,
                "exit_reason": exit_reason,
                "notional": position["notional"],
            }
        )
        position = None
    if position is not None:
        i = len(data) - 1
        raw_exit = float(arrays["close"][i])
        fees = position["notional"] * (TAKER_FEE + SLIPPAGE) * 2
        funding_pnl, funding_events = _funding_pnl(
            funding,
            position["entry_ts"],
            timestamps[i],
            position["side"],
            position["notional"],
        )
        gross = (
            position["notional"] * (raw_exit / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - raw_exit / position["entry_price"])
        )
        trades.append(
            {
                "side": position["side"],
                "entry_ts": position["entry_ts"],
                "exit_ts": timestamps[i],
                "gross_pnl": gross,
                "fees": fees,
                "funding": funding_pnl,
                "funding_events": funding_events,
                "net_pnl": gross - fees + funding_pnl,
                "mae": min(0.0, gross - fees + funding_pnl),
                "hold_bars": i - position["entry_i"],
                "hold_hours": (timestamps[i] - position["entry_ts"]).total_seconds() / 3600,
                "exit_reason": "end",
                "notional": position["notional"],
            }
        )
    return {
        "strategy": "mean_reversion",
        "metrics": _metrics_from_trades(trades, timestamps[0], timestamps[-1]),
        "trades": trades,
    }


def _parameter_sets() -> list[dict[str, Any]]:
    return [
        {
            "feature": feature,
            "threshold": threshold,
            "hold_bars": hold,
            "stop_atr": stop,
            "volatility_filter": vol_filter,
            "entry_offset": 0.0005,
            "target_pct": target,
        }
        for feature, thresholds in (("bb_z", (1.0, 1.5)), ("range_pos", (0.25, 0.4)))
        for threshold, hold, stop, vol_filter, target in itertools.product(
            thresholds,
            (12, 24),
            (0.0, 3.0),
            (False, True),
            (0.001, 0.002),
        )
    ]


def _rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in rows
        if row["metrics"]["net_pnl"] > 0 and row["metrics"]["trades"] >= 20
    ]
    return sorted(
        eligible,
        key=lambda row: (
            row["metrics"]["net_pnl"] / max(1.0, row["metrics"]["max_drawdown"]),
            row["metrics"]["profit_factor"],
            row["metrics"]["net_pnl"],
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
            {
                "params": params,
                "metrics": run_mean_reversion(train, train_funding, **params)["metrics"],
            }
            for params in _parameter_sets()
        ]
        ranked = _rank(rows)
        if not ranked:
            folds.append(
                {
                    "selected_params": None,
                    "train_metrics": None,
                    "test_metrics": _metrics_from_trades(
                        [], test.index[0], test.index[-1]
                    ),
                    "test_trades": [],
                    "selection_failed": True,
                }
            )
            continue
        selected = ranked[0]
        test_result = run_mean_reversion(test, test_funding, **selected["params"])
        candidates = []
        for candidate in ranked[:5]:
            candidate_result = run_mean_reversion(test, test_funding, **candidate["params"])
            candidates.append(
                {
                    "params": candidate["params"],
                    "train_metrics": candidate["metrics"],
                    "test_metrics": candidate_result["metrics"],
                }
            )
        folds.append(
            {
                "selected_params": selected["params"],
                "train_metrics": selected["metrics"],
                "test_metrics": test_result["metrics"],
                "test_candidates": candidates,
                "test_trades": [
                    {
                        **trade,
                        "entry_ts": trade["entry_ts"].isoformat(),
                        "exit_ts": trade["exit_ts"].isoformat(),
                    }
                    for trade in test_result["trades"]
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
    return {
        "symbol": symbol,
        "strategy": "mean_reversion",
        "folds": folds,
        "oos_metrics": _metrics_from_trades(
            trades,
            min(frame.index[0], trades[0]["entry_ts"]) if trades else frame.index[0],
            max(frame.index[-1], trades[-1]["exit_ts"]) if trades else frame.index[-1],
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default="ETH-USDT-SWAP,CRV-USDT-SWAP,FARTCOIN-USDT-SWAP",
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/research"))
    parser.add_argument("--out", type=Path, default=Path("data/research/mean_reversion_report.json"))
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = []
    for symbol in [value.strip() for value in args.symbols.split(",") if value.strip()]:
        one_minute = _load_candles(symbol, args.days, args.cache_dir)
        frame = _resample(one_minute, "5min")
        funding = _fetch_funding(symbol, args.days, args.cache_dir)
        result = walk_forward(
            symbol,
            frame,
            funding,
            train_days=args.train_days,
            test_days=args.test_days,
        )
        report.append(result)
        metrics = result["oos_metrics"]
        print(
            f"{symbol} mean_reversion: trades={metrics['trades']} "
            f"net={metrics['net_pnl']:.4f} pf={metrics['profit_factor']:.3f} "
            f"dd={metrics['max_drawdown']:.4f} "
            f"rolling14={metrics['rolling_14d_positive_pct']:.1f}%"
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
