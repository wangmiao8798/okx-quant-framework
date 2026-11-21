#!/usr/bin/env python3
"""Scan simple predictive features before building another live strategy."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


ROUND_TRIP_COST = 0.0014


def _load(symbol: str, days: int, root: Path) -> pd.DataFrame:
    rows = json.loads((root / f"{symbol}_{days}d_1m.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    frame = frame.sort_values("ts").drop_duplicates("ts").set_index("ts")
    return (
        frame.resample("5min", label="left", closed="left")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def _features(frame: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(index=frame.index)
    close = frame["close"]
    returns = np.log(close).diff()
    out["mom1"] = close.pct_change(1)
    out["mom3"] = close.pct_change(3)
    out["mom6"] = close.pct_change(6)
    out["mom12"] = close.pct_change(12)
    out["mom24"] = close.pct_change(24)
    for span in (10, 20, 50, 100):
        ema = close.ewm(span=span, adjust=False).mean()
        out[f"ema_gap_{span}"] = close / ema - 1.0
    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    out["bb_z"] = (close - sma20) / std20.replace(0, np.nan)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["rsi14"] = 100 - 100 / (1 + rs)
    previous = close.shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous).abs(),
            (frame["low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_pct"] = true_range.rolling(14).mean() / close
    volume_log = np.log1p(frame["volume"])
    out["volume_z"] = (
        (volume_log - volume_log.rolling(96).mean())
        / volume_log.rolling(96).std().replace(0, np.nan)
    )
    high20 = frame["high"].rolling(20).max()
    low20 = frame["low"].rolling(20).min()
    out["range_pos"] = (close - low20) / (high20 - low20).replace(0, np.nan) - 0.5
    out["realized_vol"] = returns.rolling(24).std()
    out["hour_sin"] = np.sin(2 * np.pi * out.index.hour / 24)
    out["hour_cos"] = np.cos(2 * np.pi * out.index.hour / 24)
    return out


def _evaluate(
    features: pd.DataFrame,
    close: pd.Series,
    *,
    horizon: int,
    train_ratio: float,
) -> list[dict]:
    forward = close.shift(-horizon) / close - 1.0
    split = int(len(features) * train_ratio)
    results = []
    for name in features.columns:
        x = features[name]
        y = forward
        train_mask = (x.index < features.index[split]) & x.notna() & y.notna()
        test_mask = (x.index >= features.index[split]) & x.notna() & y.notna()
        if train_mask.sum() < 500 or test_mask.sum() < 200:
            continue
        train_ic = float(x[train_mask].corr(y[train_mask], method="spearman"))
        test_ic = float(x[test_mask].corr(y[test_mask], method="spearman"))
        train_quantiles = pd.qcut(x[train_mask], 5, labels=False, duplicates="drop")
        direction = 0
        if not isinstance(train_quantiles, int) and len(train_quantiles):
            top = y[train_mask][train_quantiles == train_quantiles.max()].mean()
            bottom = y[train_mask][train_quantiles == train_quantiles.min()].mean()
            direction = 1 if top >= bottom else -1
        if direction == 0:
            continue
        test_x = x[test_mask] * direction
        test_y = y[test_mask]
        quantiles = pd.qcut(test_x, 5, labels=False, duplicates="drop")
        top_mask = quantiles == quantiles.max()
        bottom_mask = quantiles == quantiles.min()
        top_return = float(test_y[top_mask].mean())
        bottom_return = float(test_y[bottom_mask].mean())
        long_short = top_return - bottom_return
        long_only = top_return - ROUND_TRIP_COST
        short_only = -bottom_return - ROUND_TRIP_COST
        best_side = max(long_only, short_only)
        results.append(
            {
                "feature": name,
                "horizon_bars": horizon,
                "direction": direction,
                "train_ic": round(train_ic, 5),
                "test_ic": round(test_ic, 5),
                "test_top_gross": round(top_return, 6),
                "test_bottom_gross": round(bottom_return, 6),
                "test_long_short_gross": round(long_short, 6),
                "test_long_net": round(long_only, 6),
                "test_short_net": round(short_only, 6),
                "test_best_side_net": round(best_side, 6),
                "train_observations": int(train_mask.sum()),
                "test_observations": int(test_mask.sum()),
            }
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbols",
        default="XAU-USDT-SWAP,ETH-USDT-SWAP,CRV-USDT-SWAP,FARTCOIN-USDT-SWAP,DRAM-USDT-SWAP",
    )
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/research"))
    parser.add_argument("--out", type=Path, default=Path("data/research/alpha_feature_scan.json"))
    parser.add_argument("--horizons", default="1,3,6,12,24")
    parser.add_argument("--train-ratio", type=float, default=0.7)
    parser.add_argument("--top", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = []
    for symbol in [value.strip() for value in args.symbols.split(",") if value.strip()]:
        frame = _load(symbol, args.days, args.cache_dir)
        feature_frame = _features(frame)
        rows = []
        for horizon in [int(value) for value in args.horizons.split(",") if value.strip()]:
            rows.extend(
                _evaluate(
                    feature_frame,
                    frame["close"],
                    horizon=horizon,
                    train_ratio=args.train_ratio,
                )
            )
        ranked = sorted(
            rows,
            key=lambda row: (
                row["test_best_side_net"] > 0,
                row["test_best_side_net"],
                row["test_ic"] * row["direction"],
            ),
            reverse=True,
        )
        report.append({"symbol": symbol, "results": ranked})
        print(
            f"{symbol}: candidates={len(rows)} "
            f"best_net={ranked[0]['test_best_side_net']:.5f} "
            f"best_feature={ranked[0]['feature']} h={ranked[0]['horizon_bars']}"
            if ranked
            else f"{symbol}: no candidates"
        )
        for row in ranked[: args.top]:
            print(
                "  "
                f"{row['feature']:14s} h={row['horizon_bars']:2d} "
                f"dir={row['direction']:+d} train_ic={row['train_ic']:+.4f} "
                f"test_ic={row['test_ic']:+.4f} net={row['test_best_side_net']:+.5f} "
                f"top={row['test_top_gross']:+.5f} bottom={row['test_bottom_gross']:+.5f}"
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report={args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
