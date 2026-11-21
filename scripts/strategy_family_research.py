#!/usr/bin/env python3
"""Independent walk-forward research for trend and pullback strategies."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import statistics
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


TAKER_FEE = 0.0005
MAKER_FEE = 0.0002
SLIPPAGE = 0.0002
INITIAL_EQUITY = 1000.0
RISK_PER_TRADE = 5.0
MAX_TRADE_NOTIONAL = 300.0


def _load_candles(symbol: str, days: int, root: Path) -> pd.DataFrame:
    path = root / f"{symbol}_{days}d_1m.json"
    if not path.exists():
        raise FileNotFoundError(path)
    rows = json.loads(path.read_text(encoding="utf-8"))
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    frame = frame.sort_values("ts").drop_duplicates("ts").set_index("ts")
    return frame[["open", "high", "low", "close", "volume"]]


def _resample(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    result = frame.resample(rule, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return result.dropna()


def _fetch_funding(symbol: str, days: int, cache_dir: Path) -> pd.DataFrame:
    cache_path = cache_dir / f"{symbol}_funding_{days}d.json"
    if cache_path.exists():
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        from okx.PublicData import PublicAPI

        api = PublicAPI("", "", "", flag="0")
        rows: list[dict[str, Any]] = []
        after = str(int(time.time() * 1000))
        target = days * 3 + 10
        while len(rows) < target:
            response = api.funding_rate_history(instId=symbol, after=after, limit="100")
            if str(response.get("code", "")) != "0":
                raise RuntimeError(f"{symbol} funding fetch failed: {response}")
            batch = response.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            oldest = min(int(item["fundingTime"]) for item in batch)
            if oldest >= int(after):
                break
            after = str(oldest)
            time.sleep(0.12)
        deduped = {int(item["fundingTime"]): item for item in rows}
        rows = [deduped[key] for key in sorted(deduped)]
        cache_path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    frame = pd.DataFrame(
        [
            {
                "ts": int(item["fundingTime"]),
                "funding_rate": float(item.get("realizedRate") or item.get("fundingRate") or 0.0),
            }
            for item in rows
        ]
    )
    if frame.empty:
        return pd.DataFrame(columns=["funding_rate"])
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    return frame.sort_values("ts").drop_duplicates("ts").set_index("ts")[["funding_rate"]]


def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    close = out["close"]
    previous = close.shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - previous).abs(),
            (out["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = true_range.rolling(14).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100 - (100 / (1 + rs))
    return out


def _funding_pnl(
    funding: pd.DataFrame,
    entry_ts: pd.Timestamp,
    exit_ts: pd.Timestamp,
    side: str,
    notional: float,
) -> tuple[float, int]:
    if funding.empty:
        return 0.0, 0
    slice_ = funding.loc[(funding.index > entry_ts) & (funding.index <= exit_ts)]
    if slice_.empty:
        return 0.0, 0
    sign = 1.0 if side == "long" else -1.0
    pnl = -sign * float(slice_["funding_rate"].sum()) * notional
    return pnl, int(len(slice_))


def _metrics_from_trades(
    trades: list[dict[str, Any]],
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    initial_equity: float = INITIAL_EQUITY,
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
            "equity": initial_equity,
        }
    equity = initial_equity
    peak = equity
    max_dd = 0.0
    trough_index = 0
    recovery_index = None
    curve = []
    for index, trade in enumerate(trades):
        worst_equity = equity + min(0.0, float(trade["mae"]))
        if worst_equity < peak:
            drawdown = peak - worst_equity
            if drawdown > max_dd:
                max_dd = drawdown
                trough_index = index
                recovery_index = None
        equity += float(trade["net_pnl"])
        peak = max(peak, equity)
        if recovery_index is None and equity >= peak:
            recovery_index = index
        curve.append({"ts": trade["exit_ts"], "pnl": trade["net_pnl"], "equity": equity})
    if recovery_index is None:
        recovery_days = (end_ts - trades[trough_index]["exit_ts"]).total_seconds() / 86400
    else:
        recovery_days = (
            trades[recovery_index]["exit_ts"] - trades[trough_index]["exit_ts"]
        ).total_seconds() / 86400
    gross_profit = sum(max(0.0, float(t["gross_pnl"])) for t in trades)
    gross_loss = abs(sum(min(0.0, float(t["gross_pnl"])) for t in trades))
    net_values = [float(t["net_pnl"]) for t in trades]
    fees = sum(float(t["fees"]) for t in trades)
    funding = sum(float(t["funding"]) for t in trades)
    daily = pd.Series(
        [float(t["net_pnl"]) for t in trades],
        index=pd.DatetimeIndex([t["exit_ts"] for t in trades]),
    ).resample("1D").sum()
    rolling_14d = daily.rolling("14D").sum().dropna()
    exposure_bars = sum(int(t["hold_bars"]) for t in trades)
    total_bars = max(1, int((end_ts - start_ts).total_seconds() / 300))
    return {
        "trades": len(trades),
        "net_pnl": sum(net_values),
        "gross_pnl": sum(float(t["gross_pnl"]) for t in trades),
        "fees": fees,
        "funding": funding,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0),
        "win_rate_pct": sum(value > 0 for value in net_values) / len(net_values) * 100,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd / initial_equity * 100,
        "recovery_days": recovery_days,
        "avg_trade": statistics.fmean(net_values),
        "avg_hold_hours": statistics.fmean(float(t["hold_hours"]) for t in trades),
        "long_net": sum(float(t["net_pnl"]) for t in trades if t["side"] == "long"),
        "short_net": sum(float(t["net_pnl"]) for t in trades if t["side"] == "short"),
        "fee_to_gross_pct": fees / gross_profit * 100 if gross_profit > 0 else 999.0,
        "daily_positive_pct": (daily > 0).mean() * 100 if len(daily) else 0.0,
        "rolling_14d_positive_pct": (rolling_14d > 0).mean() * 100 if len(rolling_14d) else 0.0,
        "exposure_pct": exposure_bars / total_bars * 100 if total_bars else 0.0,
        "equity": equity,
    }


def run_breakout(
    frame: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    breakout_window: int,
    fast: int,
    slow: int,
    stop_atr: float,
    tp_atr: float,
    max_hold_bars: int,
    cooldown_bars: int = 0,
    risk_usdt: float = RISK_PER_TRADE,
) -> dict[str, Any]:
    data = _indicators(frame)
    data["ema_fast"] = data["close"].ewm(span=fast, adjust=False).mean()
    data["ema_slow"] = data["close"].ewm(span=slow, adjust=False).mean()
    data["break_high"] = data["high"].shift(1).rolling(breakout_window).max()
    data["break_low"] = data["low"].shift(1).rolling(breakout_window).min()
    arrays = {name: data[name].to_numpy() for name in data.columns}
    timestamps = data.index
    position = None
    next_entry_i = 0
    trades: list[dict[str, Any]] = []
    for i in range(1, len(data)):
        if position is None and i >= next_entry_i:
            signal_index = i - 1
            long_signal = (
                arrays["close"][signal_index] > arrays["break_high"][signal_index]
                and arrays["ema_fast"][signal_index] > arrays["ema_slow"][signal_index]
                and np.isfinite(arrays["atr14"][signal_index])
            )
            short_signal = (
                arrays["close"][signal_index] < arrays["break_low"][signal_index]
                and arrays["ema_fast"][signal_index] < arrays["ema_slow"][signal_index]
                and np.isfinite(arrays["atr14"][signal_index])
            )
            if long_signal or short_signal:
                side = "long" if long_signal else "short"
                raw_entry = float(arrays["open"][i])
                atr = float(arrays["atr14"][signal_index])
                stop_distance = stop_atr * atr
                if raw_entry > 0 and stop_distance > 0:
                    stop_rate = stop_distance / raw_entry
                    notional = min(MAX_TRADE_NOTIONAL, risk_usdt / max(stop_rate, 1e-6))
                    entry_price = raw_entry
                    stop = raw_entry - stop_distance if side == "long" else raw_entry + stop_distance
                    target = (
                        raw_entry + tp_atr * atr if side == "long" else raw_entry - tp_atr * atr
                    )
                    position = {
                        "side": side,
                        "entry_i": i,
                        "entry_ts": timestamps[i],
                        "entry_price": entry_price,
                        "raw_entry": raw_entry,
                        "notional": notional,
                        "stop": stop,
                        "target": target,
                        "atr": atr,
                        "min_price": raw_entry,
                        "max_price": raw_entry,
                    }
            continue
        if position is None:
            continue

        low = float(arrays["low"][i])
        high = float(arrays["high"][i])
        close = float(arrays["close"][i])
        position["min_price"] = min(position["min_price"], low)
        position["max_price"] = max(position["max_price"], high)
        exit_reason = ""
        raw_exit = 0.0
        if position["side"] == "long":
            if low <= position["stop"]:
                raw_exit = min(float(arrays["open"][i]), position["stop"])
                exit_reason = "stop"
            elif high >= position["target"]:
                raw_exit = max(float(arrays["open"][i]), position["target"])
                exit_reason = "target"
            elif arrays["ema_fast"][i] < arrays["ema_slow"][i]:
                raw_exit = close
                exit_reason = "trend_exit"
        else:
            if high >= position["stop"]:
                raw_exit = max(float(arrays["open"][i]), position["stop"])
                exit_reason = "stop"
            elif low <= position["target"]:
                raw_exit = min(float(arrays["open"][i]), position["target"])
                exit_reason = "target"
            elif arrays["ema_fast"][i] > arrays["ema_slow"][i]:
                raw_exit = close
                exit_reason = "trend_exit"
        if not exit_reason and i - position["entry_i"] >= max_hold_bars:
            raw_exit = close
            exit_reason = "time"
        if not exit_reason:
            continue
        cost = TAKER_FEE + SLIPPAGE
        exit_price = raw_exit
        gross = (
            position["notional"] * (exit_price / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - exit_price / position["entry_price"])
        )
        fees = position["notional"] * ((TAKER_FEE + SLIPPAGE) * 2)
        funding_pnl, funding_events = _funding_pnl(
            funding,
            position["entry_ts"],
            timestamps[i],
            position["side"],
            position["notional"],
        )
        worst_price = position["min_price"] if position["side"] == "long" else position["max_price"]
        mae_price = worst_price
        mae = (
            position["notional"] * (mae_price / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - mae_price / position["entry_price"])
        )
        mae = min(0.0, mae - fees + funding_pnl)
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
                "mae": mae,
                "hold_bars": i - position["entry_i"],
                "hold_hours": (timestamps[i] - position["entry_ts"]).total_seconds() / 3600,
                "exit_reason": exit_reason,
                "notional": position["notional"],
            }
        )
        position = None
        next_entry_i = i + cooldown_bars
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
                "exit_reason": "end",
                "notional": position["notional"],
            }
        )
    return {
        "strategy": "trend_breakout",
        "metrics": _metrics_from_trades(trades, timestamps[0], timestamps[-1]),
        "trades": trades,
    }


def run_pullback(
    frame: pd.DataFrame,
    funding: pd.DataFrame,
    *,
    fast: int,
    slow: int,
    pullback_ema: int,
    rsi_max: float,
    stop_atr: float,
    tp_atr: float,
    max_hold_bars: int,
    cooldown_bars: int = 0,
    risk_usdt: float = RISK_PER_TRADE,
) -> dict[str, Any]:
    data = _indicators(frame)
    data["ema_fast"] = data["close"].ewm(span=fast, adjust=False).mean()
    data["ema_slow"] = data["close"].ewm(span=slow, adjust=False).mean()
    data["ema_pullback"] = data["close"].ewm(span=pullback_ema, adjust=False).mean()
    arrays = {name: data[name].to_numpy() for name in data.columns}
    timestamps = data.index
    position = None
    next_entry_i = 0
    trades: list[dict[str, Any]] = []
    for i in range(1, len(data)):
        if position is None and i >= next_entry_i:
            signal = i - 1
            long_signal = (
                arrays["ema_fast"][signal] > arrays["ema_slow"][signal]
                and arrays["low"][signal] <= arrays["ema_pullback"][signal]
                and arrays["close"][signal] > arrays["ema_pullback"][signal]
                and arrays["rsi14"][signal] < rsi_max
                and np.isfinite(arrays["atr14"][signal])
            )
            short_signal = (
                arrays["ema_fast"][signal] < arrays["ema_slow"][signal]
                and arrays["high"][signal] >= arrays["ema_pullback"][signal]
                and arrays["close"][signal] < arrays["ema_pullback"][signal]
                and arrays["rsi14"][signal] > 100 - rsi_max
                and np.isfinite(arrays["atr14"][signal])
            )
            if long_signal or short_signal:
                side = "long" if long_signal else "short"
                raw_entry = float(arrays["open"][i])
                atr = float(arrays["atr14"][signal])
                stop_distance = stop_atr * atr
                if raw_entry > 0 and stop_distance > 0:
                    stop_rate = stop_distance / raw_entry
                    notional = min(MAX_TRADE_NOTIONAL, risk_usdt / max(stop_rate, 1e-6))
                    entry_price = raw_entry
                    stop = raw_entry - stop_distance if side == "long" else raw_entry + stop_distance
                    target = raw_entry + tp_atr * atr if side == "long" else raw_entry - tp_atr * atr
                    position = {
                        "side": side,
                        "entry_i": i,
                        "entry_ts": timestamps[i],
                        "entry_price": entry_price,
                        "raw_entry": raw_entry,
                        "notional": notional,
                        "stop": stop,
                        "target": target,
                        "min_price": raw_entry,
                        "max_price": raw_entry,
                    }
            continue
        if position is None:
            continue
        low = float(arrays["low"][i])
        high = float(arrays["high"][i])
        close = float(arrays["close"][i])
        position["min_price"] = min(position["min_price"], low)
        position["max_price"] = max(position["max_price"], high)
        exit_reason = ""
        raw_exit = 0.0
        if position["side"] == "long":
            if low <= position["stop"]:
                raw_exit = min(float(arrays["open"][i]), position["stop"])
                exit_reason = "stop"
            elif high >= position["target"]:
                raw_exit = max(float(arrays["open"][i]), position["target"])
                exit_reason = "target"
            elif arrays["ema_fast"][i] < arrays["ema_slow"][i]:
                raw_exit = close
                exit_reason = "trend_exit"
        else:
            if high >= position["stop"]:
                raw_exit = max(float(arrays["open"][i]), position["stop"])
                exit_reason = "stop"
            elif low <= position["target"]:
                raw_exit = min(float(arrays["open"][i]), position["target"])
                exit_reason = "target"
            elif arrays["ema_fast"][i] > arrays["ema_slow"][i]:
                raw_exit = close
                exit_reason = "trend_exit"
        if not exit_reason and i - position["entry_i"] >= max_hold_bars:
            raw_exit = close
            exit_reason = "time"
        if not exit_reason:
            continue
        cost = TAKER_FEE + SLIPPAGE
        exit_price = raw_exit
        gross = (
            position["notional"] * (exit_price / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - exit_price / position["entry_price"])
        )
        fees = position["notional"] * cost * 2
        funding_pnl, funding_events = _funding_pnl(
            funding,
            position["entry_ts"],
            timestamps[i],
            position["side"],
            position["notional"],
        )
        worst_price = position["min_price"] if position["side"] == "long" else position["max_price"]
        mae_price = worst_price
        mae = (
            position["notional"] * (mae_price / position["entry_price"] - 1.0)
            if position["side"] == "long"
            else position["notional"] * (1.0 - mae_price / position["entry_price"])
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
        next_entry_i = i + cooldown_bars
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
                "exit_reason": "end",
                "notional": position["notional"],
            }
        )
    return {
        "strategy": "trend_pullback",
        "metrics": _metrics_from_trades(trades, timestamps[0], timestamps[-1]),
        "trades": trades,
    }


def _parameter_sets(strategy: str) -> list[dict[str, Any]]:
    if strategy == "trend_breakout":
        return [
            {
                "breakout_window": window,
                "fast": fast,
                "slow": slow,
                "stop_atr": stop,
                "tp_atr": tp,
                "max_hold_bars": hold,
                "cooldown_bars": cooldown,
            }
            for window, (fast, slow), stop, tp, hold, cooldown in itertools.product(
                (60, 120),
                ((10, 50), (20, 100)),
                (1.5, 2.5),
                (4.0, 6.0),
                (288,),
                (24,),
            )
        ]
    return [
        {
            "fast": fast,
            "slow": slow,
            "pullback_ema": pullback,
            "rsi_max": rsi,
            "stop_atr": stop,
            "tp_atr": tp,
            "max_hold_bars": hold,
            "cooldown_bars": cooldown,
        }
        for (fast, slow), pullback, rsi, stop, tp, hold, cooldown in itertools.product(
            ((10, 50), (20, 100)),
            (10, 20),
            (45.0, 55.0),
            (1.5, 2.5),
            (2.5, 4.0),
            (288,),
            (24,),
        )
    ]


def _rank_parameters(train_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in train_rows
        if row["metrics"]["trades"] >= 8 and row["metrics"]["net_pnl"] > 0
    ]
    if not eligible:
        return []
    return sorted(
        eligible,
        key=lambda row: (
            row["metrics"]["net_pnl"] / max(10.0, row["metrics"]["max_drawdown"]),
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
    strategy: str,
    train_days: int,
    test_days: int,
) -> dict[str, Any]:
    bars_per_day = 24 * 12
    train_bars = train_days * bars_per_day
    test_bars = test_days * bars_per_day
    step = test_bars
    folds = []
    for start in range(0, len(frame) - train_bars - test_bars + 1, step):
        train = frame.iloc[start : start + train_bars]
        test = frame.iloc[start + train_bars : start + train_bars + test_bars]
        train_funding = funding.loc[
            (funding.index >= train.index[0]) & (funding.index <= train.index[-1])
        ]
        test_funding = funding.loc[
            (funding.index >= test.index[0]) & (funding.index <= test.index[-1])
        ]
        rows = []
        for params in _parameter_sets(strategy):
            runner = run_breakout if strategy == "trend_breakout" else run_pullback
            result = runner(train, train_funding, **params)
            rows.append({"params": params, "metrics": result["metrics"]})
        ranked = _rank_parameters(rows)
        if not ranked:
            folds.append(
                {
                    "start": train.index[0].isoformat(),
                    "train_end": train.index[-1].isoformat(),
                    "test_start": test.index[0].isoformat(),
                    "test_end": test.index[-1].isoformat(),
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
        runner = run_breakout if strategy == "trend_breakout" else run_pullback
        test_result = runner(test, test_funding, **selected["params"])
        test_candidates = []
        for candidate in ranked[:5]:
            candidate_result = runner(test, test_funding, **candidate["params"])
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
                "train_end": train.index[-1].isoformat(),
                "test_start": test.index[0].isoformat(),
                "test_end": test.index[-1].isoformat(),
                "selected_params": selected["params"],
                "train_metrics": selected["metrics"],
                "test_metrics": test_result["metrics"],
                "test_candidates": test_candidates,
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
    oos_trades = []
    for fold in folds:
        for trade in fold["test_trades"]:
            item = dict(trade)
            item["entry_ts"] = pd.Timestamp(item["entry_ts"])
            item["exit_ts"] = pd.Timestamp(item["exit_ts"])
            oos_trades.append(item)
    oos_metrics = _metrics_from_trades(
        oos_trades,
        min(frame.index[0], oos_trades[0]["entry_ts"]) if oos_trades else frame.index[0],
        max(frame.index[-1], oos_trades[-1]["exit_ts"]) if oos_trades else frame.index[-1],
    )
    return {
        "symbol": symbol,
        "strategy": strategy,
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
    parser.add_argument("--out", type=Path, default=Path("data/research/strategy_family_report.json"))
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = []
    for symbol in [item.strip() for item in args.symbols.split(",") if item.strip()]:
        one_minute = _load_candles(symbol, args.days, args.cache_dir)
        five_minute = _resample(one_minute, "5min")
        funding = _fetch_funding(symbol, args.days, args.cache_dir)
        for strategy in ("trend_breakout", "trend_pullback"):
            result = walk_forward(
                symbol,
                five_minute,
                funding,
                strategy=strategy,
                train_days=args.train_days,
                test_days=args.test_days,
            )
            report.append(result)
            metrics = result["oos_metrics"]
            print(
                f"{symbol} {strategy}: trades={metrics['trades']} net={metrics['net_pnl']:.4f} "
                f"pf={metrics['profit_factor']:.3f} dd={metrics['max_drawdown']:.4f} "
                f"recovery={metrics['recovery_days']:.2f}d "
                f"rolling14={metrics['rolling_14d_positive_pct']:.1f}%"
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
