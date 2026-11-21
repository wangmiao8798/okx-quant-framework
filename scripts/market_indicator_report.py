#!/usr/bin/env python3
"""Read-only OKX market indicator report for live strategy diagnostics.

This sidecar fetches public OKX candles/books/funding data, computes simple
TradingView-style indicators, and combines them with local strategy status.
It does NOT read secrets, place orders, cancel orders, or modify strategy state.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_CONFIGS = [
    Path("config/config.yaml"),
    Path("config/config_dram.yaml"),
    Path("config/config_fartcoin.yaml"),
]
DEFAULT_BARS = ["5m", "15m", "1H", "4H"]
OKX_BASE = "https://www.okx.com"


def fetch_json(path: str, params: Dict[str, str], timeout: int = 15) -> Dict[str, Any]:
    url = OKX_BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 okx-quant-market-indicator/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def extract_symbol_from_config(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("symbol:"):
                symbol = stripped.split(":", 1)[1].strip().strip('"\'')
                return symbol or None
    except Exception:
        return None
    return None


def discover_symbols(configs: Iterable[Path], status_path: Path) -> List[str]:
    symbols: List[str] = []
    for cfg in configs:
        sym = extract_symbol_from_config(cfg)
        if sym and sym not in symbols:
            symbols.append(sym)
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            for row in (status.get("strategies") or {}).values():
                if isinstance(row, dict):
                    sym = row.get("symbol") or row.get("instId") or row.get("inst_id")
                    if sym and sym not in symbols:
                        symbols.append(sym)
        except Exception:
            pass
    return symbols


def fetch_candles(symbol: str, bar: str, limit: int) -> List[Dict[str, float]]:
    data = fetch_json("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)})
    rows = data.get("data") or []
    candles = []
    # OKX returns newest first. Reverse to chronological order for indicators.
    for row in reversed(rows):
        if len(row) < 6:
            continue
        candles.append(
            {
                "ts": int(safe_float(row[0])),
                "open": safe_float(row[1]),
                "high": safe_float(row[2]),
                "low": safe_float(row[3]),
                "close": safe_float(row[4]),
                "vol": safe_float(row[5]),
            }
        )
    return candles


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = value * alpha + current * (1.0 - alpha)
    return current


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for prev, cur in zip(values[-period - 1 : -1], values[-period:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles: List[Dict[str, float]], period: int = 14) -> Optional[float]:
    if len(candles) <= period:
        return None
    trs: List[float] = []
    prev_close = candles[-period - 1]["close"]
    for candle in candles[-period:]:
        high = candle["high"]
        low = candle["low"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = candle["close"]
    return sum(trs) / period


def macd(values: List[float]) -> Dict[str, Optional[float]]:
    if len(values) < 35:
        return {"macd": None, "signal": None, "hist": None}
    ema12_series: List[float] = []
    ema26_series: List[float] = []
    for idx in range(len(values)):
        sub = values[: idx + 1]
        e12 = ema(sub, 12)
        e26 = ema(sub, 26)
        if e12 is not None and e26 is not None:
            ema12_series.append(e12)
            ema26_series.append(e26)
    macd_series = [a - b for a, b in zip(ema12_series[-len(ema26_series) :], ema26_series)]
    if not macd_series:
        return {"macd": None, "signal": None, "hist": None}
    sig = ema(macd_series, 9)
    m = macd_series[-1]
    return {"macd": m, "signal": sig, "hist": (m - sig) if sig is not None else None}


def bollinger(values: List[float], period: int = 20, std_mult: float = 2.0) -> Dict[str, Optional[float]]:
    if len(values) < period:
        return {"mid": None, "upper": None, "lower": None, "bandwidth_pct": None, "position": None}
    window = values[-period:]
    mid = sum(window) / period
    sd = statistics.pstdev(window)
    upper = mid + std_mult * sd
    lower = mid - std_mult * sd
    close = values[-1]
    width_pct = (upper - lower) / mid * 100.0 if mid else None
    pos = (close - lower) / (upper - lower) if upper > lower else None
    return {"mid": mid, "upper": upper, "lower": lower, "bandwidth_pct": width_pct, "position": pos}


def realized_volatility(values: List[float], lookback: int = 48) -> Optional[float]:
    if len(values) <= lookback:
        return None
    rets = []
    for prev, cur in zip(values[-lookback - 1 : -1], values[-lookback:]):
        if prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(len(rets)) * 100.0


def fetch_orderbook(symbol: str, depth: int = 5) -> Dict[str, Any]:
    data = fetch_json("/api/v5/market/books", {"instId": symbol, "sz": str(depth)})
    row = (data.get("data") or [{}])[0]
    bids = [(safe_float(x[0]), safe_float(x[1])) for x in row.get("bids", []) if len(x) >= 2]
    asks = [(safe_float(x[0]), safe_float(x[1])) for x in row.get("asks", []) if len(x) >= 2]
    if not bids or not asks:
        return {"ok": False, "error": data.get("msg") or "missing bid/ask"}
    bid, ask = bids[0][0], asks[0][0]
    mid = (bid + ask) / 2.0
    bid_depth = sum(px * sz for px, sz in bids)
    ask_depth = sum(px * sz for px, sz in asks)
    spread_pct = (ask - bid) / mid * 100.0 if mid else 0.0
    imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)
    return {
        "ok": True,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth_notional": bid_depth,
        "ask_depth_notional": ask_depth,
        "imbalance": imbalance,
    }


def fetch_funding(symbol: str) -> Dict[str, Any]:
    try:
        data = fetch_json("/api/v5/public/funding-rate", {"instId": symbol})
        row = (data.get("data") or [{}])[0]
        return {
            "ok": bool(row),
            "funding_rate": safe_float(row.get("fundingRate")),
            "next_funding_time": int(safe_float(row.get("nextFundingTime"))),
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def classify_market(close: float, ema20: Optional[float], ema50: Optional[float], atr_pct: Optional[float], rsi14: Optional[float], bb_pos: Optional[float]) -> Tuple[str, List[str]]:
    notes: List[str] = []
    trend = "range"
    if ema20 and ema50 and close > ema20 > ema50:
        trend = "uptrend"
        notes.append("price_above_ema20_ema50")
    elif ema20 and ema50 and close < ema20 < ema50:
        trend = "downtrend"
        notes.append("price_below_ema20_ema50")
    else:
        notes.append("ema_mixed_or_flat")
    if atr_pct is not None:
        if atr_pct >= 2.0:
            notes.append("very_high_atr")
        elif atr_pct >= 1.0:
            notes.append("high_atr")
        else:
            notes.append("normal_atr")
    if rsi14 is not None:
        if rsi14 >= 70:
            notes.append("rsi_hot")
        elif rsi14 <= 30:
            notes.append("rsi_cold")
    if bb_pos is not None:
        if bb_pos >= 0.9:
            notes.append("near_upper_band")
        elif bb_pos <= 0.1:
            notes.append("near_lower_band")
    return trend, notes


def grid_advice(trend: str, atr_pct: Optional[float], spread_pct: Optional[float], funding: Dict[str, Any]) -> Dict[str, Any]:
    warnings: List[str] = []
    positives: List[str] = []
    score = 70.0
    if atr_pct is not None:
        if atr_pct > 2.0:
            score -= 18
            warnings.append("波动过高，网格加仓容易被单边行情拉开")
        elif atr_pct < 0.25:
            score -= 8
            warnings.append("波动偏低，成交/止盈机会可能不足")
        else:
            positives.append("波动处于可观察区间")
    if trend in ("uptrend", "downtrend"):
        score -= 10
        warnings.append("短线趋势较明确，双向网格需注意弱侧恢复和层数")
    else:
        positives.append("均线结构偏震荡/混合，较适合网格观察")
    if spread_pct is not None:
        if spread_pct > 0.08:
            score -= 12
            warnings.append("盘口价差偏大，需谨慎缩小止盈间距")
        else:
            positives.append("盘口价差正常")
    fr = funding.get("funding_rate") if funding.get("ok") else None
    if fr is not None and abs(fr) > 0.0005:
        score -= 5
        warnings.append("资金费率偏高，持仓成本需要纳入判断")
    score = max(0.0, min(100.0, score))
    if score >= 75:
        label = "good_for_grid"
    elif score >= 55:
        label = "watch"
    else:
        label = "caution"
    return {"score": round(score, 1), "label": label, "positives": positives, "warnings": warnings}


def analyze_symbol(symbol: str, bars: List[str], limit: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {"symbol": symbol, "bars": {}}
    orderbook = fetch_orderbook(symbol)
    funding = fetch_funding(symbol)
    result["orderbook"] = round_numbers(orderbook)
    result["funding"] = round_numbers(funding)
    for bar in bars:
        try:
            candles = fetch_candles(symbol, bar, limit)
            closes = [c["close"] for c in candles]
            if len(closes) < 30:
                raise RuntimeError(f"not enough candles: {len(closes)}")
            close = closes[-1]
            e20 = ema(closes, 20)
            e50 = ema(closes, 50)
            r = rsi(closes, 14)
            a = atr(candles, 14)
            atr_pct = (a / close * 100.0) if a and close else None
            bb = bollinger(closes)
            m = macd(closes)
            vol = realized_volatility(closes)
            trend, notes = classify_market(close, e20, e50, atr_pct, r, bb.get("position"))
            advice = grid_advice(trend, atr_pct, orderbook.get("spread_pct") if orderbook.get("ok") else None, funding)
            result["bars"][bar] = round_numbers(
                {
                    "ok": True,
                    "candles": len(candles),
                    "last_ts": candles[-1]["ts"],
                    "close": close,
                    "ema20": e20,
                    "ema50": e50,
                    "rsi14": r,
                    "atr14": a,
                    "atr_pct": atr_pct,
                    "macd": m,
                    "bollinger": bb,
                    "realized_vol_pct": vol,
                    "market_regime": trend,
                    "notes": notes,
                    "grid_advice": advice,
                }
            )
        except Exception as exc:
            result["bars"][bar] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return result


def round_numbers(obj: Any) -> Any:
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 8)
    if isinstance(obj, dict):
        return {k: round_numbers(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_numbers(v) for v in obj]
    return obj


def load_strategy_status(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": "missing status file"}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        strategies = {}
        for name, row in (raw.get("strategies") or {}).items():
            if not isinstance(row, dict):
                continue
            strategies[name] = {
                "symbol": row.get("symbol") or row.get("instId") or row.get("inst_id"),
                "mode": row.get("mode"),
                "running": row.get("running"),
                "halted": row.get("halted"),
                "last_event_type": row.get("last_event_type"),
                "last_event_detail": row.get("last_event_detail"),
                "long_qty": row.get("long_qty"),
                "short_qty": row.get("short_qty"),
                "margin_ratio": row.get("margin_ratio"),
                "tp_orders": row.get("tp_orders"),
            }
        return {
            "ok": True,
            "updated_at": raw.get("updated_at"),
            "running": raw.get("running"),
            "paused": raw.get("paused"),
            "strategy_count": raw.get("strategy_count"),
            "halted_count": raw.get("halted_count"),
            "strategies": strategies,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only OKX market indicator report")
    parser.add_argument("--symbols", default="", help="comma-separated symbols; default discovers from configs/status")
    parser.add_argument("--bars", default=",".join(DEFAULT_BARS), help="comma-separated OKX bar names")
    parser.add_argument("--limit", type=int, default=200, help="candles per bar")
    parser.add_argument("--status", type=Path, default=Path("data/strategies_status.json"))
    parser.add_argument("--out", type=Path, default=Path("data/market_indicator_report.json"))
    parser.add_argument("--summary", action="store_true", help="print compact human summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bars = [b.strip() for b in args.bars.split(",") if b.strip()]
    if args.symbols.strip():
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = discover_symbols(DEFAULT_CONFIGS, args.status)
    if not symbols:
        raise SystemExit("no symbols discovered; pass --symbols")
    payload = {
        "generated_at": int(time.time()),
        "source": "okx_public_api_read_only",
        "note": "diagnostics only; no order/trading action",
        "symbols": {},
        "strategy_status": load_strategy_status(args.status),
    }
    for symbol in symbols:
        try:
            payload["symbols"][symbol] = analyze_symbol(symbol, bars, args.limit)
        except Exception as exc:
            payload["symbols"][symbol] = {"symbol": symbol, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(args.out))
    if args.summary:
        for symbol, row in payload["symbols"].items():
            ob = row.get("orderbook") or {}
            bar5 = (row.get("bars") or {}).get("5m") or next(iter((row.get("bars") or {}).values()), {})
            if not bar5.get("ok"):
                print(f"{symbol}: ERROR {bar5.get('error') or row.get('error')}")
                continue
            advice = bar5.get("grid_advice") or {}
            print(
                f"{symbol}: close={bar5.get('close')} regime={bar5.get('market_regime')} "
                f"rsi={bar5.get('rsi14')} atr%={bar5.get('atr_pct')} spread%={ob.get('spread_pct')} "
                f"score={advice.get('score')} label={advice.get('label')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
