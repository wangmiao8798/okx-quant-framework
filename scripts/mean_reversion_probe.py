#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mean_reversion_probe.py — 均值回归信号验证（离线统计分析）
--------------------------------------------------------
只读 OKX 公共行情，不触碰实盘账户、不下单、不读密钥。

回答三个问题：
  1. 各品种是否存在统计上显著的均值回归？
  2. 最优触发偏离度（几个标准差 σ）是多少？
  3. 回归速度（几小时内回归）有多快？

方法：
  - 拉取各品种 1h K 线，约 90 天
  - 计算滚动均值 MA / 标准差 STD → z-score = (close - MA) / STD
  - 对每个「偏离事件」( |z| >= 阈值 )，统计未来 T 小时的价格回归行为
  - 输出：回归概率 + 反向开仓期望收益（未扣手续费）+ 回归程度
"""
import json
import sys
import time
import urllib.request

INSTS = {
    "CRV": "CRV-USDT-SWAP",
    "FARTCOIN": "FARTCOIN-USDT-SWAP",
    "RAM": "RAM-USDT-SWAP",
}
BAR = "1H"
DAYS = 90
MA_WINDOWS = [24, 48, 96]      # 1h bar 对应的 1/2/4 天均线
Z_THRESHOLDS = [1.0, 1.5, 2.0]  # 触发偏离度（σ）
HORIZONS = [4, 12, 24]          # 未来回归窗口（小时）
FEE = 0.001                     # 参考双边 taker 手续费 0.10%


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
    return dedup


def mean(xs):
    return sum(xs) / len(xs)


def stdev(xs):
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return var ** 0.5


def analyze(closes, ma_win, z_th, horizon):
    """
    返回 (事件数, 朝均线方向移动概率, 完全回归概率, 反向开仓平均收益%, 平均回归程度)
    - 朝均线方向移动概率: 未来 T 小时价格朝均线方向移动的事件占比
    - 完全回归概率: 价格收回 >= 一半偏离距离的事件占比
    - 反向开仓收益: 偏离时反向开仓、T 小时后平仓的平均收益率(未扣手续费)
    - 回归程度: 平均收回偏离距离的比例
    """
    n = len(closes)
    if n < ma_win + horizon + 10:
        return 0, 0.0, 0.0, 0.0, 0.0
    rev_cnt = 0      # 朝均线方向移动
    half_cnt = 0     # 收回 >= 一半偏离
    rev_ret = 0.0
    rev_deg = 0.0
    cnt = 0
    for i in range(ma_win, n - horizon):
        win = closes[i - ma_win:i]
        sd = stdev(win)
        if sd <= 0:
            continue
        z = (closes[i] - mean(win)) / sd
        if abs(z) < z_th:
            continue
        direction = 1 if z > 0 else -1    # +1 偏高→做空, -1 偏低→做多
        dev = closes[i] - mean(win)        # 有符号偏离距离
        future = closes[i + horizon]
        ret = -direction * (future - closes[i]) / closes[i]   # 反向开仓收益
        deg = (closes[i] - future) * direction / abs(dev) if dev != 0 else 0.0
        cnt += 1
        rev_ret += ret
        rev_deg += deg
        if (closes[i] - future) * direction > 0:
            rev_cnt += 1
        if deg >= 0.5:
            half_cnt += 1
    if cnt == 0:
        return 0, 0.0, 0.0, 0.0, 0.0
    return cnt, rev_cnt / cnt, half_cnt / cnt, rev_ret / cnt * 100, rev_deg / cnt


def main():
    print("=" * 92)
    print("均值回归信号验证报告")
    print(f"品种: {list(INSTS)} | K线: {BAR} | 历史: {DAYS} 天 | 参考双边手续费: {FEE*100:.2f}%")
    print("=" * 92)
    report = {}
    for name, inst in INSTS.items():
        print(f"\n>>> 拉取 {name} ({inst}) ...")
        candles = fetch_candles(inst)
        if not candles:
            print(f"  [SKIP] {name} 无数据")
            continue
        closes = [float(c[4]) for c in candles]
        print(f"  数据点: {len(closes)} 根 1h K线, 约 {len(closes)/24:.0f} 天")
        report[name] = {"inst": inst, "bars": len(closes)}
        for mw in MA_WINDOWS:
            report[name][mw] = {}
            for zt in Z_THRESHOLDS:
                report[name][mw][zt] = {}
                for h in HORIZONS:
                    cnt, prob, half, ret, deg = analyze(closes, mw, zt, h)
                    report[name][mw][zt][h] = {
                        "n": cnt, "prob": prob, "half": half, "ret": ret, "deg": deg,
                    }

    for name in report:
        print(f"\n{'='*92}\n{name} ({report[name]['inst']})  — 数据 {report[name]['bars']} 根\n{'='*92}")
        print(f"{'MA(h)':>5} | {'σ':>4} | {'样本':>6} | "
              f"{'4h回归%':>8} | {'12h回归%':>8} | {'24h回归%':>8} | "
              f"{'24h半回归%':>9} | {'反开收益%':>9}")
        print("-" * 92)
        for mw in MA_WINDOWS:
            for zt in Z_THRESHOLDS:
                r4 = report[name][mw][zt][4]
                r12 = report[name][mw][zt][12]
                r24 = report[name][mw][zt][24]
                print(f"{mw:>5} | {zt:>4.1f} | {r24['n']:>6} | "
                      f"{r4['prob']*100:>7.1f}% | {r12['prob']*100:>7.1f}% | {r24['prob']*100:>7.1f}% | "
                      f"{r24['half']*100:>8.1f}% | {r24['ret']:>+8.2f}%")

    print(f"\n{'='*92}\n品种综合评分（均值回归适合度，按 24h 反开收益排序）\n{'='*92}")
    print(f"{'品种':>10} | {'最佳MA':>6} | {'最佳σ':>5} | {'样本':>6} | {'24h回归%':>8} | "
          f"{'反开收益%':>8} | {'扣费后%':>7} | {'评级'}")
    print("-" * 92)
    scored = []
    for name in report:
        best = None
        best_score = -1e9
        for mw in MA_WINDOWS:
            for zt in Z_THRESHOLDS:
                r = report[name][mw][zt][24]
                if r["n"] < 30:
                    continue
                if r["ret"] > best_score:
                    best_score = r["ret"]
                    best = (mw, zt, r)
        if best:
            mw, zt, r = best
            net = r["ret"] - FEE * 100
            grade = "★强" if net > 0.15 else ("★中" if net > 0 else "✗弱")
            scored.append((name, mw, zt, r, net, grade))
    scored.sort(key=lambda x: -x[3]["ret"])
    for name, mw, zt, r, net, grade in scored:
        print(f"{name:>10} | {mw:>6} | {zt:>5.1f} | {r['n']:>6} | {r['prob']*100:>7.1f}% | "
              f"{r['ret']:>+8.2f}% | {net:>+6.2f}% | {grade}")

    print("\n" + "=" * 92)
    print("解读口径:")
    print("  - 回归% = 未来 T 小时价格「朝均线方向移动」的事件占比；>50% 才算有回归倾向，越高越显著")
    print("  - 反开收益% = 偏离 σ 时反向开仓、持 T 小时平仓的平均收益（未扣手续费，参考双边 0.10%）")
    print("  - 反开收益% 为正且 > 手续费，才说明该品种该偏离度下有可重复的均值回归 alpha")
    print("  - 评级: ★强(扣费后>+0.15%) / ★中(>0) / ✗弱(<=0，不适合网格)")
    print("=" * 92)

    with open("data/mean_reversion_report.json", "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已保存 data/mean_reversion_report.json")


if __name__ == "__main__":
    main()
