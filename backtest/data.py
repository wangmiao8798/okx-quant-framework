"""回测数据：从OKX获取历史K线并缓存到CSV"""
import os, csv, time
from typing import List, Optional
from datetime import datetime, timedelta
from utils.helpers import safe_float

BARS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

def fetch_candles(symbol: str, bar: str = "1h", limit: int = 200,
                  market_api=None) -> List[dict]:
    """从OKX拉取K线数据，返回 [{ts,open,high,low,close,vol}]"""
    if not market_api:
        return []
    resp = market_api.get_candlesticks(instId=symbol, bar=bar, limit=str(limit))
    candles = []
    for item in resp.get("data", []):
        candles.append({
            "ts": int(item[0]),
            "open": safe_float(item[1]),
            "high": safe_float(item[2]),
            "low": safe_float(item[3]),
            "close": safe_float(item[4]),
            "vol": safe_float(item[5]),
        })
    candles.sort(key=lambda x: x["ts"])
    return candles

def fetch_multi_batch(symbol: str, bar: str, total_bars: int,
                      market_api=None) -> List[dict]:
    """分批拉取更多历史数据"""
    all_candles = []
    limit = min(300, total_bars)
    after = ""
    while len(all_candles) < total_bars:
        kwargs = {"instId": symbol, "bar": bar, "limit": str(limit)}
        if after:
            kwargs["after"] = after
        resp = market_api.get_candlesticks(**kwargs)
        batch = resp.get("data", [])
        if not batch:
            break
        for item in batch:
            all_candles.append({
                "ts": int(item[0]),
                "open": safe_float(item[1]),
                "high": safe_float(item[2]),
                "low": safe_float(item[3]),
                "close": safe_float(item[4]),
                "vol": safe_float(item[5]),
            })
        after = batch[-1][0]
        time.sleep(0.1)
    all_candles.sort(key=lambda x: x["ts"])
    return all_candles[:total_bars]

def save_to_csv(candles: List[dict], path: str):
    """保存K线到CSV"""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp","open","high","low","close","volume"])
        for c in candles:
            w.writerow([c["ts"], c["open"], c["high"], c["low"], c["close"], c["vol"]])

def load_from_csv(path: str) -> List[dict]:
    """从CSV加载K线"""
    candles = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "ts": int(row["timestamp"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "vol": float(row["volume"]),
            })
    return candles
