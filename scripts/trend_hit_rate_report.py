#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trend_hit_rate_report.py — ADX 趋势判断「理论命中率」与「实盘触发」对照报表
=========================================================================
每日 / 每周自动跑（systemd timer）：
  - 理论命中率：对活跃品种重算近 90 天 1h K 线的 ADX 趋势判断命中率
    （回测前向验证法），分别按 TREND_ADX=25 与 MOCA 实盘阈值 28 两套，
    确保同源对照。
  - 实盘触发（两路并排，按用户选择「两者都统计对照」）：
      * MOCA 观察态趋势判定事件（来自 trend_events.db.moca_events，
        apply_live=false 下的「若上线会触发」候选次数 + 趋势态持续占比）
      * throttle_dominant 主导边净敞口封锁事件（来自 throttle_events，
        实盘真正生效的趋势风控，封锁次数 + 封锁时长占比）
  - 输出 data/reports/trend_hit_rate_{mode}_{date}.{json,md}
  - 可选 --email 复用 utils.notifier 发送 Markdown 报表

用法：
  python3 scripts/trend_hit_rate_report.py                 # 每日报表（默认）
  python3 scripts/trend_hit_rate_report.py --mode weekly
  python3 scripts/trend_hit_rate_report.py --mode daily --email
"""
import argparse
import datetime as dt
import glob
import json
import os
import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "backtest"))

DB_PATH = PROJECT / "data" / "trend_events.db"
REPORTS_DIR = PROJECT / "data" / "reports"

# MOCA 实盘 trend_strength 阈值（config thresholds.trend_strength），
# 与回测默认 TREND_ADX=25 不同，报表里按 28 重算一遍做同源对照。
MOCA_TREND_STRENGTH = 28.0

TRENDING_STATES = ("trending_up", "trending_down")


def _import_hedge_grid():
    import hedge_grid as hg
    return hg


def active_symbols():
    """扫描 config/config_*.yaml（排除 .disabled/.bak）取 futures.symbol。"""
    try:
        import yaml
        syms = []
        for p in sorted(glob.glob(str(PROJECT / "config" / "config*.yaml"))):
            if p.endswith(".disabled") or ".bak" in os.path.basename(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                s = (cfg.get("futures") or {}).get("symbol")
                if s and s not in syms:
                    syms.append(s)
            except Exception:
                continue
        if syms:
            return syms
    except Exception:
        pass
    # fallback：回测内置品种字典
    try:
        hg = _import_hedge_grid()
        return list(hg.INSTS.values())
    except Exception:
        return []


def theoretical_hit_rate(symbol):
    """重算近 90 天理论命中率，返回 dict(25/28) 或带 error 的 dict。"""
    hg = _import_hedge_grid()
    try:
        bars = hg.fetch_candles(symbol)
    except Exception as e:
        return {"error": f"fetch failed: {e}"}
    if not bars:
        return {"error": "no bars"}
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    zs = hg.rolling_z(closes, hg.MA_WINDOW)
    adxs = hg.wilder_adx(highs, lows, closes, 14)
    hr25 = hg.compute_trend_hit_rate(bars, zs, adxs)
    hr28 = hg.compute_trend_hit_rate(bars, zs, adxs, adx_threshold=MOCA_TREND_STRENGTH)
    return {
        "bars": len(bars),
        "last": round(closes[-1], 6),
        "hit_rate_25": hr25,
        "hit_rate_28": hr28,
    }


def window_bounds(mode):
    now = dt.datetime.now()
    if mode == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - dt.timedelta(days=1)
        end = start + dt.timedelta(days=1)
    else:  # weekly：上周一 00:00 ~ 本周一 00:00
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        while monday.weekday() != 0:
            monday -= dt.timedelta(days=1)
        end = monday
        start = monday - dt.timedelta(days=7)
    return start, end


def read_window_events(start_ts, end_ts):
    if not DB_PATH.exists():
        return [], []
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        try:
            moca = conn.execute(
                "SELECT ts, symbol, state, confidence, apply_live FROM moca_events "
                "WHERE ts>=? AND ts<? ORDER BY ts", (start_ts, end_ts)
            ).fetchall()
        except sqlite3.OperationalError:
            moca = []
        try:
            thr = conn.execute(
                "SELECT ts, symbol, side, action, net, soft FROM throttle_events "
                "WHERE ts>=? AND ts<? ORDER BY ts", (start_ts, end_ts)
            ).fetchall()
        except sqlite3.OperationalError:
            thr = []
        return moca, thr
    finally:
        conn.close()


def summarize_moca(rows, window_sec):
    by_sym = {}
    for ts, sym, state, conf, al in rows:
        by_sym.setdefault(sym, []).append((ts, state))
    out = {}
    for sym, evs in by_sym.items():
        evs.sort()
        states = {}
        trending_sec = 0.0
        prev_ts = None
        prev_state = None
        for ts, state in evs:
            states[state] = states.get(state, 0) + 1
            if prev_ts is not None and prev_state in TRENDING_STATES:
                trending_sec += min(ts - prev_ts, window_sec)
            prev_ts, prev_state = ts, state
        trending_events = states.get("trending_up", 0) + states.get("trending_down", 0)
        out[sym] = {
            "events": len(evs),
            "trending_events": trending_events,
            "spike_events": states.get("spike", 0),
            "calm_events": states.get("calm", 0),
            "states": states,
            "trending_duration_pct": round(trending_sec / window_sec * 100, 1) if window_sec else 0.0,
        }
    return out


def summarize_throttle(rows, window_sec):
    by_sym = {}
    for ts, sym, side, action, net, soft in rows:
        by_sym.setdefault(sym, []).append((ts, action))
    out = {}
    for sym, evs in by_sym.items():
        evs.sort()
        blocked_sec = 0.0
        blocked_cnt = 0
        released_cnt = 0
        prev_ts = None
        prev_action = None
        for ts, action in evs:
            if action == "blocked":
                blocked_cnt += 1
            elif action == "released":
                released_cnt += 1
            if prev_ts is not None and prev_action == "blocked":
                blocked_sec += min(ts - prev_ts, window_sec)
            prev_ts, prev_action = ts, action
        out[sym] = {
            "blocked_events": blocked_cnt,
            "released_events": released_cnt,
            "blocked_duration_pct": round(blocked_sec / window_sec * 100, 1) if window_sec else 0.0,
        }
    return out


def build_report(mode):
    start, end = window_bounds(mode)
    start_ts, end_ts = start.timestamp(), end.timestamp()
    window_sec = end_ts - start_ts
    symbols = active_symbols()

    theoretical = {}
    for sym in symbols:
        theoretical[sym] = theoretical_hit_rate(sym)

    moca_rows, thr_rows = read_window_events(start_ts, end_ts)
    live_moca = summarize_moca(moca_rows, window_sec)
    live_throttle = summarize_throttle(thr_rows, window_sec)

    report = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": mode,
        "window": {
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "days": round(window_sec / 86400, 2),
        },
        "moca_apply_live": False,
        "theoretical": theoretical,
        "live_moca_events": live_moca,
        "live_throttle_events": live_throttle,
        "note": ("MOCA apply_live=false：观察态趋势判定为「若上线会触发」的候选次数，"
                 "非真实停手。throttle_dominant 为实盘真正生效的趋势风控。"),
    }
    return report


def render_markdown(report):
    w = report["window"]
    L = []
    L.append(f"# ADX 趋势判断命中率 · 实盘触发对照报表（{report['mode']}）")
    L.append("")
    L.append(f"- 生成时间：{report['generated_at']}")
    L.append(f"- 统计窗口：{w['start']} ~ {w['end']}（{w['days']} 天）")
    L.append(f"- MOCA apply_live：**{report['moca_apply_live']}**（观察态，记录候选触发）")
    L.append("")

    L.append("## 一、理论命中率（回测前向验证，近 90 天 1h K）")
    L.append("")
    L.append("| 品种 | K线数 | 现价 | 对称命中(25) | 逆势命中(25) | 对称命中(28) | 逆势命中(28) | 召回率(25) |")
    L.append("|---|---|---|---|---|---|---|---|")
    for sym, t in report["theoretical"].items():
        if not isinstance(t, dict) or "hit_rate_25" not in t:
            L.append(f"| {sym} | — | — | 行情失败 | — | — | — | — |")
            continue
        h25 = t["hit_rate_25"]
        h28 = t["hit_rate_28"]
        L.append(
            f"| {sym} | {t['bars']} | {t['last']} | {h25['regime_precision']}% | "
            f"{h25['dir_precision']}% | {h28['regime_precision']}% | {h28['dir_precision']}% | "
            f"{h25['recall']}% |"
        )
    L.append("")
    L.append("* 对称命中=ADX 判趋势后确实走出强移动（停手即正确）；逆势命中=方向也对（只停逆势腿）。")
    L.append("* 25=回测默认阈值，28=MOCA 实盘 `trend_strength`，并列做同源对照。")
    L.append("")

    L.append("## 二、实盘 MOCA 观察态趋势判定（候选触发）")
    L.append("")
    L.append("| 品种 | 总事件 | 趋势事件 | Spike | Calm | 趋势态时长占比 |")
    L.append("|---|---|---|---|---|---|")
    if report["live_moca_events"]:
        for sym, m in report["live_moca_events"].items():
            L.append(
                f"| {sym} | {m['events']} | {m['trending_events']} | {m['spike_events']} | "
                f"{m['calm_events']} | {m['trending_duration_pct']}% |"
            )
    else:
        L.append("| — | 0 | 0 | 0 | 0 | 0% |（窗口内无 MOCA 事件；观察态每 30s 分析，状态变化才记录）")
    L.append("")

    L.append("## 三、实盘 throttle_dominant 主导边封锁（真实生效）")
    L.append("")
    L.append("| 品种 | 封锁事件 | 解除事件 | 封锁时长占比 |")
    L.append("|---|---|---|---|")
    if report["live_throttle_events"]:
        for sym, t in report["live_throttle_events"].items():
            L.append(
                f"| {sym} | {t['blocked_events']} | {t['released_events']} | {t['blocked_duration_pct']}% |"
            )
    else:
        L.append("| — | 0 | 0 | 0% |（窗口内无主导边封锁）")
    L.append("")
    L.append(f"> 说明：{report['note']}")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    ap.add_argument("--email", action="store_true", help="通过 SMTP 发送 Markdown 报表")
    ap.add_argument("--print", action="store_true", help="打印 Markdown 到 stdout")
    args = ap.parse_args()

    report = build_report(args.mode)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_tag = dt.datetime.now().strftime("%Y%m%d")
    json_path = REPORTS_DIR / f"trend_hit_rate_{args.mode}_{date_tag}.json"
    md_path = REPORTS_DIR / f"trend_hit_rate_{args.mode}_{date_tag}.md"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    md = render_markdown(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print(f"[report] 已写出 {json_path}")
    print(f"[report] 已写出 {md_path}")
    if args.print:
        print("\n" + md)
    if args.email:
        try:
            from utils.notifier import Notifier
            subj = f"[OKX] ADX趋势命中率报表({args.mode}) {date_tag}"
            Notifier().send_email(subj, md)
            print("[report] 已发送邮件")
        except Exception as e:
            print(f"[report] 邮件发送失败: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
