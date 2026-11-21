#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hedge_grid.py — 第三阶段：多空对冲永续网格回测
============================================================
目的：在离线环境验证「② 波动率择时 + ④ 统计因子」能否改善现网均值回归网格的
      「单边化 / 趋势亏损」问题。

三种触发模式（其余机制完全一致，仅触发条件不同）：
  S0 "fixed"          固定间距网格（现网逻辑）：跌了 step 开多、涨了 step 开空
  S1 "zscore"         2σ 偏离触发（④ 统计因子）：|z| >= z_th 才反向开仓
  S2 "zscore_trend"   2σ 偏离 + 强趋势停手（④+② 择时）：ADX 超阈值时不开新仓

机制抽象（与现网一致的关键要素）：
  - 多/空双腿各自维护层叠持仓（layer），层大小 base * mult^idx
  - 每层独立止盈：多单 p>=entry*(1+tp) 平，空单 p<=entry*(1-tp) 平
  - 净敞口上限 max_net / 单边上限 max_long_side / max_short_side
  - 每 bar 每边至多开 1 层（等价现网「每次评估一单」）
  - 双边 taker 手续费 fee_rate（开、平各收一次）

只读 OKX 公共行情，不碰密钥、不下单。
"""
import json
import math
import sys
import time
import urllib.request
from typing import List, Dict, Optional

INSTS = {
    "CRV": "CRV-USDT-SWAP",
    "FARTCOIN": "FARTCOIN-USDT-SWAP",
    "RAM": "RAM-USDT-SWAP",
}

# 现网配置快照（config/config.yaml / config_ram.yaml / config_fartcoin.yaml）
# base = base_position_usdt（保证金，U）；每层名义 = base * lev
PROFILES = {
    "CRV": dict(base=3.0, lev=5, step=0.00159, tp=0.01,
                max_net=2200, max_long=2000, max_short=1400),
    "RAM": dict(base=16.0, lev=5, step=0.01, tp=0.008,
                max_net=600, max_long=1200, max_short=1200),
    "FARTCOIN": dict(base=0.6, lev=10, step=0.004, tp=0.008,
                     max_net=150, max_long=300, max_short=300),
}

BAR = "1H"
DAYS = 90
FEE = 0.001            # taker 双边各 0.10%
INITIAL_EQUITY = 5000.0  # 参考权益（仅用于回撤/ROI 归一化）
MA_WINDOW = 24         # z-score 滚动窗口（1h bar → 1 天）
Z_THRESHOLD = 2.0      # ④ 偏离阈值（CRV 数据最优档）
TREND_ADX = 25.0       # ② 趋势停手阈值（Wilder ADX）

# 趋势判断命中率统计参数（前向验证法）
HIT_LOOKAHEAD = 12      # 向前验证窗口（bar 数；1h bar → 12h）
HIT_MIN_MOVE = 0.015    # 噪声阈值：前向收益绝对值 >= 此值才视为「真趋势」，否则视为震荡（假信号）


def _get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if i == retries - 1:
                print(f"  [ERR] {url} -> {e}", file=sys.stderr)
                return None
            time.sleep(1 + i)
    return None


def fetch_candles(inst, bar=BAR, days=DAYS):
    """拉取历史 K 线，返回升序 [(ts, open, high, low, close, vol), ...]"""
    base = "https://www.okx.com/api/v5/market"
    out = []
    r = _get(f"{base}/candles?instId={inst}&bar={bar}&limit=300")
    if r and r.get("data"):
        out.extend(r["data"])
    target = days * 24
    guard = 0
    while len(out) < target and guard < 40:
        guard += 1
        earliest = out[-1][0]
        r = _get(f"{base}/history-candles?instId={inst}&bar={bar}&after={earliest}&limit=100")
        if not r or not r.get("data"):
            break
        out.extend(r["data"])
        if len(r["data"]) < 100:
            break
    seen = set()
    dedup = []
    for c in out:
        if c[0] not in seen:
            seen.add(c[0])
            dedup.append(c)
    dedup.sort(key=lambda x: int(x[0]))
    # ts, open, high, low, close, vol
    return [(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in dedup]


def mean(xs):
    return sum(xs) / len(xs)


def stdev(xs):
    m = mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5 if len(xs) > 1 else 0.0


def rolling_z(closes, win):
    """返回 z-score 序列（前 win-1 位置为 None）"""
    zs = [None] * len(closes)
    for i in range(win, len(closes)):
        w = closes[i - win:i]
        sd = stdev(w)
        if sd <= 0:
            zs[i] = 0.0
        else:
            zs[i] = (closes[i] - mean(w)) / sd
    return zs


def wilder_adx(highs, lows, closes, period=14):
    """Wilder ADX 序列（前若干位置为 None）。趋势强度：ADX>=25 视为强趋势。"""
    n = len(closes)
    adx = [None] * n
    if n < period * 2 + 1:
        return adx
    tr = [0.0] * n
    pdm = [0.0] * n
    ndm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    # Wilder 平滑
    tr_s = sum(tr[1:period + 1])
    pdm_s = sum(pdm[1:period + 1])
    ndm_s = sum(ndm[1:period + 1])
    atr = tr_s / period
    pdi = pdm_s / period
    ndi = ndm_s / period
    dxs = [None] * n
    if atr > 0:
        dx = 100.0 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0.0
        dxs[period] = dx
    for i in range(period + 1, n):
        tr_s = tr_s - tr_s / period + tr[i]
        pdm_s = pdm_s - pdm_s / period + pdm[i]
        ndm_s = ndm_s - ndm_s / period + ndm[i]
        atr = tr_s / period
        pdi = pdm_s / period
        ndi = ndm_s / period
        dx = 100.0 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) > 0 else 0.0
        dxs[i] = dx
    # ADX = Wilder 平滑 DX
    adx_s = None
    adx_prev = None
    start = period * 2
    if start >= n:
        return adx
    # 初始 DX 平均
    first = [d for d in dxs[period:start] if d is not None]
    if first:
        adx_prev = sum(first) / len(first)
        adx[start - 1] = adx_prev
    for i in range(start, n):
        if dxs[i] is None:
            continue
        adx_prev = (adx_prev * (period - 1) + dxs[i]) / period
        adx[i] = adx_prev
    return adx


def compute_trend_hit_rate(bars, zs, adxs, lookahead=HIT_LOOKAHEAD, min_move=HIT_MIN_MOVE, adx_threshold=TREND_ADX):
    """统计 ADX 趋势强度过滤器（ADX>=TREND_ADX 判强趋势）的命中率。

    验证法（前向标签）：对每个被 ADX 判为「强趋势」的 bar i，向未来
    lookahead 个 bar 看前向收益 fwd = close[i+L]/close[i] - 1：
      - 预测方向由该 bar 的 z-score 符号给出（策略里 ADX 与 z 配对使用）；
      - 真趋势（regime hit）：|fwd| >= min_move —— 价格确实走出一段强移动
        （对应 S2 对称停手：任何强趋势都利空均值回归，停手即正确）；
      - 方向命中（dir hit）：真趋势 且 fwd 方向与预测方向一致
        （对应 S3 逆势停手：只停逆势腿）。

    同时统计非趋势 bar（ADX<阈值）中实际走出强移动的次数，得到召回率。

    返回的字段（命中率 = 命中 / 信号数）：
      signals          被 ADX 判为强趋势且能前向验证的 bar 数
      regime_hits      其中价格确走出强移动的次数（方向无关）
      regime_precision regime_hits/signals（对称停手「判断对」的比例）
      dir_hits         方向也对的次数
      dir_precision    dir_hits/signals（逆势停手「判断对」的比例）
      chop             signals 中价格震荡（|fwd|<min_move）的次数（假信号）
      reversed         signals 中价格反走（强但反向）的次数
      missed           ADX<阈值 但价格走出强移动的 bar 数（漏判 / 假阴性）
      recall           regime_hits / (regime_hits + missed)
      avg_adx         信号处 ADX 均值
      trend_share_pct  signals / 总 bar（被标记为趋势的时间占比）
      avg_fwd_hit     regime_hits 的平均前向收益（%，含反向强移动）
      avg_fwd_miss    (chop+reversed) 的平均前向收益（%）
    """
    closes = [b[4] for b in bars]
    n = len(bars)
    signals = 0
    regime_hits = 0
    dir_hits = 0
    chop = 0
    reversed_ = 0
    missed = 0
    adx_sum = 0.0
    fwd_hit_sum = 0.0
    fwd_miss_sum = 0.0

    for i in range(n):
        adx = adxs[i] if i < len(adxs) else None
        z = zs[i] if i < len(zs) else None
        if adx is None or z is None:
            continue
        has_fwd = (i + lookahead) < n
        if adx >= adx_threshold:
            if not has_fwd:
                continue  # 无法前向验证，不计入命中率分母（保守）
            signals += 1
            adx_sum += adx
            direction = 1 if z > 0 else -1
            fwd = closes[i + lookahead] / closes[i] - 1.0
            fwd_dir = 1 if fwd > 0 else (-1 if fwd < 0 else 0)
            strong = abs(fwd) >= min_move
            if strong:
                regime_hits += 1
                fwd_hit_sum += fwd
                if fwd_dir == direction:
                    dir_hits += 1
                else:
                    reversed_ += 1
            else:
                chop += 1
                fwd_miss_sum += fwd
        else:
            if has_fwd:
                fwd = closes[i + lookahead] / closes[i] - 1.0
                if abs(fwd) >= min_move:
                    missed += 1

    miss_total = chop + reversed_
    total_real = regime_hits + missed
    return {
        "signals": signals,
        "regime_hits": regime_hits,
        "regime_precision": round(regime_hits / signals * 100, 1) if signals else 0.0,
        "dir_hits": dir_hits,
        "dir_precision": round(dir_hits / signals * 100, 1) if signals else 0.0,
        "chop": chop,
        "reversed": reversed_,
        "missed": missed,
        "recall": round(regime_hits / total_real * 100, 1) if total_real else 0.0,
        "avg_adx": round(adx_sum / signals, 1) if signals else 0.0,
        "trend_share_pct": round(signals / n * 100, 1) if n else 0.0,
        "avg_fwd_hit": round(fwd_hit_sum / regime_hits * 100, 2) if regime_hits else 0.0,
        "avg_fwd_miss": round(fwd_miss_sum / miss_total * 100, 2) if miss_total else 0.0,
    }


class Layer:
    __slots__ = ("side", "entry", "notional")

    def __init__(self, side, entry, notional):
        self.side = side        # "long" | "short"
        self.entry = entry
        self.notional = notional


class HedgeGridBacktest:
    """多空对冲永续网格回测器。

    mode: "fixed" | "zscore" | "zscore_trend"
    """

    def __init__(self, mode, base, lev, step, tp,
                 max_net, max_long, max_short, fee_rate=FEE,
                 ma_window=MA_WINDOW, z_threshold=Z_THRESHOLD, trend_adx=TREND_ADX):
        self.mode = mode
        self.base = base
        self.lev = lev
        self.layer_notional = base * lev   # 每层名义（保证金 × 杠杆）
        self.step = step
        self.tp = tp
        self.max_net = max_net
        self.max_long = max_long
        self.max_short = max_short
        self.fee_rate = fee_rate
        self.ma_window = ma_window
        self.z_threshold = z_threshold
        self.trend_adx = trend_adx

    def run(self, bars, zs, adxs):
        """bars: [(ts,o,h,l,c,vol)]; zs/adxs 对齐同长度。"""
        closes = [b[4] for b in bars]
        n = len(bars)
        long_layers: List[Layer] = []
        short_layers: List[Layer] = []
        realized = 0.0
        fees = 0.0
        trades = 0
        wins = 0
        gross_profit = 0.0
        gross_loss = 0.0
        max_dd = 0.0
        peak_eq = INITIAL_EQUITY
        max_naked = 0.0        # 最大净敞口绝对值
        max_naked_ratio = 0.0  # |net| / (long+short) 最大单边化程度
        equity_curve = []

        ref_long = None   # fixed 模式下多单参考价
        ref_short = None  # fixed 模式下空单参考价

        for i in range(n):
            p = closes[i]
            # ---- 1) 止盈：逐层检查 ----
            for layers, side in ((long_layers, "long"), (short_layers, "short")):
                kept = []
                for L in layers:
                    hit = (side == "long" and p >= L.entry * (1 + self.tp)) or \
                          (side == "short" and p <= L.entry * (1 - self.tp))
                    if hit:
                        if side == "long":
                            pnl = L.notional * (p - L.entry) / L.entry
                        else:
                            pnl = L.notional * (L.entry - p) / L.entry
                        close_fee = self.fee_rate * L.notional
                        fees += close_fee
                        realized += pnl - close_fee
                        trades += 1
                        if pnl - close_fee >= 0:
                            wins += 1
                            gross_profit += pnl - close_fee
                        else:
                            gross_loss += abs(pnl - close_fee)
                    else:
                        kept.append(L)
                if side == "long":
                    long_layers = kept
                else:
                    short_layers = kept

            # ---- 2) 标记权益 / 回撤 ----
            unreal = 0.0
            tot_long = sum(L.notional for L in long_layers)
            tot_short = sum(L.notional for L in short_layers)
            for L in long_layers:
                unreal += L.notional * (p - L.entry) / L.entry
            for L in short_layers:
                unreal += L.notional * (L.entry - p) / L.entry
            eq = INITIAL_EQUITY + realized + unreal - fees
            equity_curve.append(eq)
            if eq > peak_eq:
                peak_eq = eq
            dd = (peak_eq - eq) / peak_eq * 100.0
            if dd > max_dd:
                max_dd = dd
            net = tot_long - tot_short
            gross = tot_long + tot_short
            if abs(net) > max_naked:
                max_naked = abs(net)
            if gross > 0:
                ratio = abs(net) / gross
                if ratio > max_naked_ratio:
                    max_naked_ratio = ratio

            # ---- 3) 开仓触发 ----
            z = zs[i] if i < len(zs) else None
            adx = adxs[i] if i < len(adxs) else None
            do_long = False
            do_short = False

            if self.mode == "fixed":
                if ref_long is None:
                    ref_long = p
                if ref_short is None:
                    ref_short = p
                if p <= ref_long * (1 - self.step):
                    do_long = True
                if p >= ref_short * (1 + self.step):
                    do_short = True
            elif self.mode in ("zscore", "zscore_trend", "zscore_trend_asym"):
                if z is None:
                    pass
                elif self.mode == "zscore_trend" and adx is not None and adx >= self.trend_adx:
                    # 强趋势：完全停手（② 择时，对称）
                    do_long = do_short = False
                elif self.mode == "zscore_trend_asym":
                    # 强趋势：只停「逆势腿」，顺势腿保留（② 择时，非对称）
                    strong = adx is not None and adx >= self.trend_adx
                    trend_up = strong and z > 0
                    trend_dn = strong and z < 0
                    if z <= -self.z_threshold and not trend_dn:
                        do_long = True
                    if z >= self.z_threshold and not trend_up:
                        do_short = True
                else:
                    if z <= -self.z_threshold:
                        do_long = True
                    if z >= self.z_threshold:
                        do_short = True

            # ---- 4) 开多 ----
            if do_long:
                size = self.layer_notional
                # 净敞口/单边上限约束（仓位可累计到上限，模拟现网「网格随趋势累积库存」）
                net_after = (tot_long + size) - tot_short
                room_net = self.max_net - (net_after if net_after > 0 else 0)
                room_side = self.max_long - tot_long
                size = min(size, room_net, room_side)
                if size > 0:
                    fees += self.fee_rate * size
                    long_layers.append(Layer("long", p, size))
                    ref_long = p

            # ---- 5) 开空 ----
            if do_short:
                size = self.layer_notional
                net_after = tot_long - (tot_short + size)
                room_net = self.max_net - (abs(net_after) if net_after < 0 else 0)
                room_side = self.max_short - tot_short
                size = min(size, room_net, room_side)
                if size > 0:
                    fees += self.fee_rate * size
                    short_layers.append(Layer("short", p, size))
                    ref_short = p

        # 期末：按最后收盘价结算剩余持仓（浮盈浮亏转已实现，便于对比）。
        # 这些是「趋势里没等到回归、被迫带亏平仓」的仓位——正是网格「赢小亏大」的亏损来源，
        # 计入成交与盈亏，使胜率/盈亏比能真实反映策略质量。
        pf = closes[-1]
        for L in long_layers + short_layers:
            if L.side == "long":
                gross = L.notional * (pf - L.entry) / L.entry
            else:
                gross = L.notional * (L.entry - pf) / L.entry
            close_fee = self.fee_rate * L.notional
            pnl = gross - close_fee
            realized += gross
            fees += close_fee
            trades += 1
            if pnl >= 0:
                wins += 1
                gross_profit += pnl
            else:
                gross_loss += abs(pnl)
        final_eq = INITIAL_EQUITY + realized - fees

        return {
            "pnl": round(realized - fees, 2),
            "roi": round((final_eq - INITIAL_EQUITY) / INITIAL_EQUITY * 100, 2),
            "max_dd_pct": round(max_dd, 2),
            "max_naked_usdt": round(max_naked, 2),
            "max_naked_ratio": round(max_naked_ratio, 3),
            "trades": trades,
            "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else 99.0,
            "fees": round(fees, 2),
            "open_layers_end": len(long_layers) + len(short_layers),
        }


def load_bars_from_csv(path):
    import csv
    bars = []
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            bars.append((int(row["timestamp"]), float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"]), float(row["volume"])))
    return bars


def main():
    print("=" * 100)
    print("多空对冲永续网格回测 —— 三触发模式对比")
    print(f"K线 {BAR} × {DAYS} 天 | 参考权益 {INITIAL_EQUITY:.0f}U | 双边手续费 {FEE*100:.2f}%")
    print(f"z-score 窗口 {MA_WINDOW}h | 偏离阈值 {Z_THRESHOLD}σ | 趋势停手 ADX>={TREND_ADX:.0f}")
    print("=" * 100)

    report = {}
    for name, inst in INSTS.items():
        prof = PROFILES[name]
        print(f"\n>>> {name} ({inst}) 拉取行情 ...")
        bars = fetch_candles(inst)
        if not bars:
            print(f"  [SKIP] {name} 无数据")
            continue
        closes = [b[4] for b in bars]
        highs = [b[2] for b in bars]
        lows = [b[3] for b in bars]
        print(f"  数据 {len(bars)} 根 1h K线, 约 {len(bars)/24:.0f} 天, 现价 {closes[-1]:.4f}")
        zs = rolling_z(closes, MA_WINDOW)
        adxs = wilder_adx(highs, lows, closes, 14)
        hr = compute_trend_hit_rate(bars, zs, adxs)

        modes = {
            "S0 fixed": ("fixed", {}),
            "S1 zscore(2σ)": ("zscore", {"z_threshold": Z_THRESHOLD}),
            "S2 zscore+停手(全)": ("zscore_trend", {"z_threshold": Z_THRESHOLD, "trend_adx": TREND_ADX}),
            "S3 zscore+停手(逆势)": ("zscore_trend_asym", {"z_threshold": Z_THRESHOLD, "trend_adx": TREND_ADX}),
        }
        report[name] = {"inst": inst, "bars": len(bars), "last": closes[-1], "results": {}, "trend_hit_rate": hr}
        print(f"\n  {'模式':<22} | {'PnL(U)':>9} | {'ROI%':>7} | {'最大回撤%':>9} | "
              f"{'最大裸敞口U':>11} | {'单边化':>6} | {'成交':>5} | {'胜率%':>6} | {'PF':>5} | {'手续费U':>8}")
        print("  " + "-" * 100)
        for label, (mode, kw) in modes.items():
            bt = HedgeGridBacktest(mode, **prof, **kw)
            r = bt.run(bars, zs, adxs)
            report[name]["results"][label] = r
            print(f"  {label:<22} | {r['pnl']:>+9.2f} | {r['roi']:>+7.2f} | "
                  f"{r['max_dd_pct']:>9.2f} | {r['max_naked_usdt']:>11.2f} | "
                  f"{r['max_naked_ratio']:>6.2f} | {r['trades']:>5} | {r['win_rate']:>6.1f} | "
                  f"{r['profit_factor']:>5.2f} | {r['fees']:>8.2f}")

        # 趋势判断命中率（前向验证：ADX>=阈值 后 12h 是否真走出强移动）
        print(f"\n  [ADX 趋势判断命中率] 窗口={HIT_LOOKAHEAD}h 噪声阈值={HIT_MIN_MOVE*100:.1f}%")
        print(f"    信号数={hr['signals']:>4}  时间占比={hr['trend_share_pct']:>5.1f}%  "
              f"信号处ADX均={hr['avg_adx']:>5.1f}")
        print(f"    对称停手命中(走出了强移动) = {hr['regime_precision']:>5.1f}%  "
              f"(regime_hits={hr['regime_hits']} chop={hr['chop']} reversed={hr['reversed']})")
        print(f"    逆势停手命中(方向也对)     = {hr['dir_precision']:>5.1f}%  "
              f"(dir_hits={hr['dir_hits']})")
        print(f"    召回率(漏判)               = {hr['recall']:>5.1f}%  "
              f"(missed={hr['missed']})")
        print(f"    信号后12h均收益  命中={hr['avg_fwd_hit']:>+6.2f}%  误判={hr['avg_fwd_miss']:>+6.2f}%")

    # 汇总：每个品种最优模式
    print("\n" + "=" * 100)
    print("结论汇总：每个品种最优触发模式（按 PnL 排序）")
    print("=" * 100)
    for name in report:
        res = report[name]["results"]
        ranked = sorted(res.items(), key=lambda kv: -kv[1]["pnl"])
        best = ranked[0]
        print(f"\n{name}: 最优 = {best[0]} (PnL {best[1]['pnl']:+.2f}U, 回撤 {best[1]['max_dd_pct']:.1f}%, "
              f"裸敞口 {best[1]['max_naked_usdt']:.0f}U)")
        for label, r in ranked:
            tag = " ← 最优" if label == best[0] else ""
            print(f"    {label:<24} PnL {r['pnl']:>+9.2f}U  DD {r['max_dd_pct']:>6.1f}%  "
                  f"裸 {r['max_naked_usdt']:>7.0f}U{tag}")

    with open("data/hedge_grid_backtest_report.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已保存 data/hedge_grid_backtest_report.json")


if __name__ == "__main__":
    main()
