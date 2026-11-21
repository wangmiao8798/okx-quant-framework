#!/usr/bin/env python3
"""Backtest a delta-neutral long-spot / short-perp funding carry."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd


SPOT_TAKER = 0.0010
PERP_TAKER = 0.0005


def _fetch_history(inst_id: str, bar: str, days: int, cache: Path) -> pd.DataFrame:
    if cache.exists():
        rows = json.loads(cache.read_text(encoding="utf-8"))
    else:
        from okx.MarketData import MarketAPI

        api = MarketAPI("", "", "", flag="0")
        after = str(int(time.time() * 1000))
        cutoff = int(time.time() * 1000) - days * 86400000
        rows = []
        while True:
            response = api.get_history_candlesticks(
                instId=inst_id, bar=bar, after=after, limit="300"
            )
            if str(response.get("code", "")) != "0":
                raise RuntimeError(response)
            batch = response.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            oldest = int(batch[-1][0])
            if oldest <= cutoff:
                break
            after = str(oldest)
            time.sleep(0.12)
        deduped = {int(row[0]): row for row in rows}
        rows = [deduped[key] for key in sorted(deduped)]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    frame = pd.DataFrame(
        [
            {
                "ts": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            }
            for row in rows
        ]
    )
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    return frame.sort_values("ts").drop_duplicates("ts").set_index("ts")


def _load_perp(symbol: str, days: int, root: Path) -> pd.DataFrame:
    rows = json.loads((root / f"{symbol}_{days}d_1m.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    frame = frame.sort_values("ts").drop_duplicates("ts").set_index("ts")
    return frame["close"].resample("1h").last().dropna()


def _load_funding(symbol: str, days: int, root: Path) -> pd.DataFrame:
    rows = json.loads((root / f"{symbol}_funding_{days}d.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(rows)
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(frame["fundingTime"].astype("int64"), unit="ms", utc=True),
            "rate": pd.to_numeric(
                frame["realizedRate"].where(
                    frame["realizedRate"].notna(), frame["fundingRate"]
                ),
                errors="coerce",
            ).fillna(0.0),
        }
    ).set_index("ts")[["rate"]]


def simulate(
    spot: pd.Series,
    perp: pd.Series,
    funding: pd.DataFrame,
    *,
    notional: float,
    initial_equity: float = 1000.0,
    policy: str = "always",
    negative_exit_count: int = 3,
) -> dict:
    index = spot.index.intersection(perp.index)
    spot = spot.reindex(index)
    perp = perp.reindex(index)
    spot_returns = spot.pct_change().fillna(0.0)
    perp_returns = perp.pct_change().fillna(0.0)
    rate_by_hour = funding["rate"].reindex(index).fillna(0.0)
    holding = False
    equity = initial_equity
    pnl_series = []
    funding_income = 0.0
    delta_pnl = 0.0
    fees = 0.0
    entries = 0
    consecutive_negative = 0
    for i, ts in enumerate(index):
        rate = float(rate_by_hour.iloc[i])
        if rate < 0:
            consecutive_negative += 1
        else:
            consecutive_negative = 0
        should_hold = policy == "always" or (holding and consecutive_negative < negative_exit_count) or rate > 0
        if not holding and should_hold:
            fee = notional * (SPOT_TAKER + PERP_TAKER)
            equity -= fee
            fees += fee
            holding = True
            entries += 1
        if holding:
            delta = notional * (float(spot_returns.iloc[i]) - float(perp_returns.iloc[i]))
            income = notional * rate
            delta_pnl += delta
            funding_income += income
            equity += delta + income
        pnl_series.append(equity)
        if holding and not should_hold:
            fee = notional * (SPOT_TAKER + PERP_TAKER)
            equity -= fee
            fees += fee
            pnl_series[-1] = equity
            holding = False
    if holding:
        fee = notional * (SPOT_TAKER + PERP_TAKER)
        equity -= fee
        fees += fee
        pnl_series[-1] = equity
    curve = pd.Series(pnl_series, index=index)
    peak = curve.cummax()
    drawdown = peak - curve
    daily = curve.resample("1D").last().ffill()
    daily_returns = daily.pct_change().dropna()
    days = max(1.0, (index[-1] - index[0]).total_seconds() / 86400)
    return {
        "notional": notional,
        "net_pnl": equity - initial_equity,
        "funding_income": funding_income,
        "delta_pnl": delta_pnl,
        "fees": fees,
        "entries": entries,
        "annualized_return_pct": (equity / initial_equity - 1.0) * 365 / days * 100,
        "max_drawdown": float(drawdown.max()),
        "max_drawdown_pct": float(drawdown.max() / initial_equity * 100),
        "positive_days_pct": float((daily_returns > 0).mean() * 100),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--cache-dir", type=Path, default=Path("/opt/okx_quant/data/research"))
    parser.add_argument("--out", type=Path, default=Path("funding_carry_report.json"))
    args = parser.parse_args()
    cases = [
        ("ETH-USDT-SWAP", "ETH-USDT"),
        ("CRV-USDT-SWAP", "CRV-USDT"),
    ]
    report = []
    for perp_symbol, spot_symbol in cases:
        spot = _fetch_history(
            spot_symbol,
            "1H",
            args.days,
            args.cache_dir / f"{spot_symbol}_180d_1h.json",
        )["close"]
        perp = _load_perp(perp_symbol, args.days, args.cache_dir)
        funding = _load_funding(perp_symbol, args.days, args.cache_dir)
        rows = [
            {
                "symbol": perp_symbol,
                **simulate(
                    spot,
                    perp,
                    funding,
                    notional=notional,
                    policy=policy,
                    negative_exit_count=3,
                ),
                "policy": policy,
            }
            for notional in (500.0, 700.0, 750.0)
            for policy in ("always", "positive_buffer")
        ]
        report.extend(rows)
        for row in rows:
            print(
                f"{perp_symbol} notional={row['notional']:.0f} policy={row['policy']} "
                f"net={row['net_pnl']:+.2f}U annual={row['annualized_return_pct']:+.2f}% "
                f"funding={row['funding_income']:+.2f} delta={row['delta_pnl']:+.2f} "
                f"fees={row['fees']:.2f} dd={row['max_drawdown_pct']:.2f}%"
            )
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
