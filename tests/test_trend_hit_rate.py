#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_trend_hit_rate — 验证 compute_trend_hit_rate 的前向验证逻辑。

用完全确定的合成数据：close 基值 100，特定位置人工注入「信号 + 前向移动」，
断言命中/误判/漏判的计数与精度符合预期。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
from hedge_grid import compute_trend_hit_rate, TREND_ADX  # noqa: E402


def _build():
    n = 35
    closes = [100.0] * n  # 其余保持 100（前向收益 0 → 不计入 missed）
    # 信号点（ADX>=阈值）：adx=30
    closes[15] = 100.0; closes[27] = 110.0   # up 预测, +10% 强移动 → regime+dir hit
    closes[16] = 100.0; closes[28] = 90.0    # down 预测, -10% 强移动 → regime+dir hit
    closes[17] = 100.0; closes[29] = 99.0    # up 预测, -1% 震荡   → chop(误判)
    closes[18] = 100.0; closes[30] = 88.0    # up 预测, -12% 强反走 → regime hit + reversed
    # 漏判点（ADX<阈值 但价格强移动）：adx=10, z=+0.5
    closes[20] = 100.0; closes[32] = 108.0   # +8% 强移动但没被标记 → missed

    bars = [(i, 100.0, 100.0, 100.0, closes[i], 0.0) for i in range(n)]
    zs = [0.0] * n
    zs[15] = 0.5; zs[16] = -0.5; zs[17] = 0.5; zs[18] = 0.5; zs[20] = 0.5
    adxs = [10.0] * n
    adxs[15] = adxs[16] = adxs[17] = adxs[18] = 30.0
    return bars, zs, adxs


def test_counts():
    bars, zs, adxs = _build()
    r = compute_trend_hit_rate(bars, zs, adxs)
    assert r["signals"] == 4
    assert r["regime_hits"] == 3
    assert r["dir_hits"] == 2
    assert r["chop"] == 1
    assert r["reversed"] == 1
    assert r["missed"] == 1


def test_precision_and_recall():
    bars, zs, adxs = _build()
    r = compute_trend_hit_rate(bars, zs, adxs)
    assert r["regime_precision"] == 75.0   # 3/4
    assert r["dir_precision"] == 50.0      # 2/4
    assert r["recall"] == 75.0             # 3/(3+1)
    assert r["avg_adx"] == 30.0


def test_no_forward_data_not_counted():
    # 信号点在数据末尾、无前向窗口 → 不计入信号分母
    n = 5
    closes = [100.0] * n
    bars = [(i, 100.0, 100.0, 100.0, closes[i], 0.0) for i in range(n)]
    zs = [0.5] * n
    adxs = [30.0] * n
    r = compute_trend_hit_rate(bars, zs, adxs)
    assert r["signals"] == 0
    assert r["regime_precision"] == 0.0


def test_empty_and_none():
    # 无信号
    bars = [(i, 100.0, 100.0, 100.0, 100.0, 0.0) for i in range(30)]
    zs = [0.0] * 30
    adxs = [10.0] * 30  # 全部 < 阈值，且 close 恒 100 → 无 missed
    r = compute_trend_hit_rate(bars, zs, adxs)
    assert r["signals"] == 0
    assert r["missed"] == 0
    assert r["recall"] == 0.0


if __name__ == "__main__":
    test_counts()
    test_precision_and_recall()
    test_no_forward_data_not_counted()
    test_empty_and_none()
    print("OK")
