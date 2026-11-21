"""工具函数集合：技术指标计算、数值精度处理、文件操作"""
"""
"""

import math, time, json, os
from typing import Optional, Dict, Any, List


def truncate_float(value: float, decimals: int) -> float:
    factor = 10 ** decimals
    return math.floor(value * factor) / factor


def round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(value / step) * step


def calc_quantity_from_usdt(usdt: float, price: float, min_qty: float, step: float) -> float:
    raw = usdt / price
    qty = round_step(raw, step)
    return max(qty, min_qty)


def calc_percentage_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (current - previous) / previous


def format_time(ts=None) -> str:
    from datetime import datetime
    if ts is None:
        ts = time.time()
    elif ts > 1e12:
        ts = ts / 1000
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_json_file(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json_file(data, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    tr_values = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr_values.append(max(hl, hc, lc))
    if len(tr_values) < period:
        return 0.0
    return sum(tr_values[-period:]) / period


def calculate_sma(values, period):
    if len(values) < period:
        return 0.0
    return sum(values[-period:]) / period


def calculate_std(values, period):
    if len(values) < period:
        return 0.0
    sl = values[-period:]
    mean = sum(sl) / period
    var = sum((x - mean)**2 for x in sl) / period
    return math.sqrt(var)


def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2:
        return 0.0
    plus_dm, minus_dm, tr = [], [], []
    for i in range(1, len(closes)):
        up = highs[i] - highs[i-1]
        dn = lows[i-1] - lows[i]
        plus_dm.append(up if up > dn and up > 0 else 0)
        minus_dm.append(dn if dn > up and dn > 0 else 0)
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        tr.append(max(hl, hc, lc))
    if len(tr) < period:
        return 0.0
    atr = sum(tr[-period:]) / period
    sp = sum(plus_dm[-period:]) / period
    sm = sum(minus_dm[-period:]) / period
    if atr == 0:
        return 0.0
    pdi = sp / atr * 100
    ndi = sm / atr * 100
    dx = abs(pdi - ndi) / (pdi + ndi) * 100 if (pdi+ndi) > 0 else 0
    return dx


def detect_wick_ratio(high, low, open_px, close_px):
    body = abs(close_px - open_px)
    if body <= 0:
        return 0.0
    uw = high - max(open_px, close_px)
    lw = min(open_px, close_px) - low
    return max(uw, lw) / body
