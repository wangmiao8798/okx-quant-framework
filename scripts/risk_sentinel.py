#!/usr/bin/env python3
"""账户风险哨兵 — 浮亏 + 保证金率 双维度监控告警。

设计原则：
  1. 只读交易所 API，绝不下单、不撤单、不干预交易
  2. 多级阈值，分级告警；同一级别有冷却期，避免刷屏
  3. 状态持久化，支持跨次运行的趋势判断（浮亏恶化速度）
  4. 复用系统现有邮件通道（utils.notifier.Notifier）

判定维度：
  A. 浮亏绝对值           — 未实现盈亏的规模
  B. 浮亏相对权益占比     — 更本质的风险度量
  C. 可用保证金余量       — 还有多少缓冲
  D. 单一品种集中度       — 是否有单点风险
  E. 净敞口方向暴露       — 未对冲的方向性赌注

用法：
  python3 scripts/risk_sentinel.py            # 正常巡检
  python3 scripts/risk_sentinel.py --test-alert  # 发送测试邮件
  python3 scripts/risk_sentinel.py --quiet    # 仅写日志，不发邮件
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/opt/okx_quant")
os.chdir("/opt/okx_quant")

STATE_PATH = "data/risk_sentinel_state.json"
LOG_PATH = "logs/risk_sentinel.log"

# ── 阈值定义 ──────────────────────────────────────────────────────
# 分级：warning（关注）→ critical（需行动）
THRESHOLDS = {
    # 浮亏绝对值（USDT）
    "upnl_warn": -280.0,
    "upnl_crit": -400.0,
    # 浮亏占权益比例
    "upnl_pct_warn": 28.0,
    "upnl_pct_crit": 35.0,
    # 可用余额占总权益比例（低于此值说明缓冲不足）
    "avail_pct_warn": 40.0,
    "avail_pct_crit": 25.0,
    # 单品种浮亏占全部浮亏比例（集中度）
    "conc_warn": 60.0,
    "conc_crit": 75.0,
    "conc_min_upnl": -80.0,   # 浮亏规模下限：小于此值不判定集中度
    # 权益回撤（相对历史峰值）
    "dd_warn": 5.0,
    "dd_crit": 8.0,
    # 同一级别告警冷却（秒）
    "cooldown": 7200,
    "cooldown_crit": 14400,
    # 基准权益：回撤计算的锚点（本轮运行起始本金）
    "baseline_equity": 1000.0,
    # 对冲退化复合告警：对冲度低于此值且净敞口超过此值时告警
    "hedge_degrade_pct": 80.0,
    "hedge_net_usd": 1200.0,
}

LEVEL_ORDER = {"ok": 0, "warning": 1, "critical": 2}


def log(msg, echo=True):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    if echo:
        print(line)


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {"peak_equity": 0.0, "max_dd": 0.0, "last_alert": {},
            "history": []}


def save_state(st):
    st["history"] = st.get("history", [])[-288:]  # 保留最近 288 条
    os.makedirs("data", exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE_PATH)


def get_client():
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    sys.path.insert(0, "/opt/okx_quant/web")
    from config_manager import load_api_password_secure

    cm = CredentialManager({"exchange": {"okx": {"key_file": "data/keys.enc"}}})
    cm.set_password(load_api_password_secure())
    return OKXClient(cm.load())


def collect(client):
    """采集账户快照（只读）。"""
    raw = client.get_account_balance_raw()
    item = (raw.get("data") or [{}])[0]
    eq = float(item.get("totalEq") or 0)
    # USDT 明细
    avail = frozen = eq_ccy = 0.0
    for d in item.get("details") or []:
        if d.get("ccy") == "USDT":
            avail = float(d.get("availBal") or 0)
            frozen = float(d.get("frozenBal") or 0)
            eq_ccy = float(d.get("eq") or 0)

    positions = []
    g_upnl = g_margin = g_notional = 0.0
    net_by_sym = {}
    ls_by_sym = {}
    for p in client.get_positions():
        qty = float(p.get("pos") or 0)
        if abs(qty) < 1e-12:
            continue
        upl = float(p.get("upl") or 0)
        mark = float(p.get("markPx") or 0)
        margin = float(p.get("margin") or 0) or float(p.get("imr") or 0)
        sym = p.get("instId")
        side = p.get("posSide")
        # Prefer the exchange-provided USD notional (already ctVal-adjusted).
        # qty * mark alone is wrong for contracts whose ctVal != 1 (ETH = 0.1).
        notional = abs(float(p.get("notionalUsd") or 0))
        if notional <= 0:
            ct_val = float(p.get("ctVal") or 1.0)
            notional = abs(qty) * ct_val * mark
        g_upnl += upl
        g_margin += margin
        g_notional += notional
        net_by_sym[sym] = net_by_sym.get(sym, 0.0) + \
            (1 if side == "long" else -1) * notional
        e = ls_by_sym.setdefault(sym, {"long": 0.0, "short": 0.0})
        e["long" if side == "long" else "short"] += notional
        positions.append({"sym": sym, "side": side, "qty": qty,
                          "mark": mark, "upl": upl, "margin": margin,
                          "notional": notional,
                          "lever": float(p.get("lever") or 0)})

    hedge = []
    for sym, e in ls_by_sym.items():
        gross = e["long"] + e["short"]
        ratio = (min(e["long"], e["short"]) * 2 / gross * 100
                 if gross else 0.0)
        hedge.append({"sym": sym, "gross": gross,
                      "net": e["long"] - e["short"],
                      "ratio": ratio})
    return {"equity": eq, "avail": avail, "frozen": frozen,
            "ccy_eq": eq_ccy, "upnl": g_upnl, "margin": g_margin,
            "notional": g_notional, "positions": positions,
            "net_by_sym": net_by_sym, "hedge": hedge}


def evaluate(snap, st):
    """按阈值评估风险，返回 (level, [findings])。"""
    findings = []
    level = "ok"
    eq = snap["equity"] or 1.0
    upnl = snap["upnl"]
    avail = snap["avail"]
    T = THRESHOLDS

    def add(lv, title, detail):
        nonlocal level
        findings.append((lv, title, detail))
        if LEVEL_ORDER[lv] > LEVEL_ORDER[level]:
            level = lv

    # A. 浮亏绝对值
    if upnl <= T["upnl_crit"]:
        add("critical", "浮亏触及严重线",
            f"未实现盈亏 {upnl:+.2f} U 已跌破 {T['upnl_crit']:.0f} U 严重线")
    elif upnl <= T["upnl_warn"]:
        add("warning", "浮亏触及关注线",
            f"未实现盈亏 {upnl:+.2f} U 已跌破 {T['upnl_warn']:.0f} U 关注线")

    # B. 浮亏占权益比例
    upnl_pct = abs(upnl) / eq * 100
    if upnl < 0:
        if upnl_pct >= T["upnl_pct_crit"]:
            add("critical", "浮亏占比过高",
                f"浮亏占权益 {upnl_pct:.1f}%（严重线 {T['upnl_pct_crit']:.0f}%）")
        elif upnl_pct >= T["upnl_pct_warn"]:
            add("warning", "浮亏占比偏高",
                f"浮亏占权益 {upnl_pct:.1f}%（关注线 {T['upnl_pct_warn']:.0f}%）")

    # C. 可用余额缓冲
    avail_pct = avail / eq * 100
    if avail_pct <= T["avail_pct_crit"]:
        add("critical", "可用保证金不足",
            f"可用 {avail:.2f} U（占权益 {avail_pct:.1f}%），"
            f"低于严重线 {T['avail_pct_crit']:.0f}%")
    elif avail_pct <= T["avail_pct_warn"]:
        add("warning", "可用保证金偏低",
            f"可用 {avail:.2f} U（占权益 {avail_pct:.1f}%），"
            f"低于关注线 {T['avail_pct_warn']:.0f}%")

    # E. 单一品种浮亏集中度
    # 仅当浮亏达到一定规模时才有意义，否则小额浮亏会 100% 集中而误报
    if upnl <= T["conc_min_upnl"]:
        losses = sorted(((p["sym"], p["upl"]) for p in snap["positions"]
                         if p["upl"] < 0), key=lambda x: x[1])
        if losses:
            worst_sym, worst_upl = losses[0]
            conc = abs(worst_upl) / abs(upnl) * 100
            if conc >= T["conc_crit"]:
                add("critical", "单品种风险集中",
                    f"{worst_sym} 浮亏 {worst_upl:+.2f} U，"
                    f"占全部浮亏 {conc:.1f}%")
            elif conc >= T["conc_warn"]:
                add("warning", "单品种亏损集中",
                    f"{worst_sym} 浮亏 {worst_upl:+.2f} U，"
                    f"占全部浮亏 {conc:.1f}%")

    # D. 权益回撤（相对基准权益）
    # 冷启动时若直接用当前权益做峰值，会失去历史基准 → 用配置的基准值兜底
    baseline = st.get("baseline_equity") or T.get("baseline_equity") or 0.0
    peak = max(st.get("peak_equity") or 0, baseline, eq)
    if peak > 0:
        dd = (peak - eq) / peak * 100
        if dd >= T["dd_crit"]:
            add("critical", "权益回撤超限",
                f"从峰值 {peak:.2f} 回撤 {dd:.2f}%（严重线 {T['dd_crit']:.0f}%）")
        elif dd >= T["dd_warn"]:
            add("warning", "权益回撤偏大",
                f"从峰值 {peak:.2f} 回撤 {dd:.2f}%（关注线 {T['dd_warn']:.0f}%）")

    # F. 对冲退化 + 净敞口放大 → 资金费按单边计费加速
    for h in snap.get("hedge", []):
        if h["ratio"] < T["hedge_degrade_pct"] \
                and abs(h["net"]) > T["hedge_net_usd"]:
            burn = abs(h["net"]) * 0.0002 * 3
            add("warning", "对冲退化：资金费消耗加速",
                f"{h['sym']} 对冲 {h['ratio']:.0f}%，净敞口 "
                f"{h['net']:+.0f} USD；按 0.02%/8h 估算约 "
                f"{burn:.2f} U/天，请检查对冲腿")
            break

    return level, findings


def should_alert(st, level, findings):
    """冷却控制：同一级别+同一标题在冷却期内只发一次。"""
    now = time.time()
    cooldown = THRESHOLDS["cooldown_crit"] if level == "critical" \
        else THRESHOLDS["cooldown"]
    last = st.setdefault("last_alert", {})
    sent = []
    for lv, title, _ in findings:
        key = f"{lv}:{title}"
        if now - last.get(key, 0) >= cooldown:
            last[key] = now
            sent.append((lv, title, _))
    return sent


def format_email(snap, findings):
    lines = ["账户风险哨兵告警", ""]
    lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"账户总权益: {snap['equity']:.2f} U")
    lines.append(f"可用余额:   {snap['avail']:.2f} U")
    lines.append(f"未实现盈亏: {snap['upnl']:+.2f} U")
    lines.append(f"占用保证金: {snap['margin']:.2f} U")
    lines.append(f"持仓名义:   {snap['notional']:.2f} U")
    lines.append("")
    lines.append("触发项:")
    for lv, title, detail in findings:
        mark = "[严重]" if lv == "critical" else "[关注]"
        lines.append(f"  {mark} {title}")
        lines.append(f"        {detail}")
    lines.append("")
    lines.append("持仓明细:")
    for p in sorted(snap["positions"], key=lambda x: x["upl"]):
        lines.append(f"  {p['sym']:<20}{p['side']:<7}qty={p['qty']:<12.4f}"
                     f"开仓后浮动={p['upl']:+.2f} U")
    lines.append("")
    lines.append("净敞口（方向性暴露）:")
    for sym, v in snap["net_by_sym"].items():
        lines.append(f"  {sym:<20}{v:+.2f} USD")
    lines.append("")
    lines.append("提示: 本告警只监控不干预，不会自动交易。")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="不发邮件")
    ap.add_argument("--test-alert", action="store_true", help="发送测试邮件")
    args = ap.parse_args()

    if args.test_alert:
        try:
            from utils.notifier import Notifier
            n = Notifier()
            ok = n.send_email("[量化] 风险哨兵测试",
                              "这是一封风险哨兵测试邮件。收到即表示告警通道正常。")
            print("测试邮件发送:", "成功" if ok else "失败（检查 SMTP 配置）")
        except Exception as e:
            print("测试邮件异常:", e)
        return

    try:
        client = get_client()
        snap = collect(client)
    except Exception as e:
        log(f"采集失败: {type(e).__name__}: {e}")
        return

    st = load_state()
    st["peak_equity"] = max(st.get("peak_equity") or 0, snap["equity"])
    level, findings = evaluate(snap, st)

    log(f"巡检 权益={snap['equity']:.2f} 可用={snap['avail']:.2f} "
        f"浮亏={snap['upnl']:+.2f} 保证金={snap['margin']:.2f} "
        f"持仓={len(snap['positions'])} → {level.upper()}",
        echo=True)

    for lv, title, detail in findings:
        log(f"  [{lv}] {title}: {detail}")

    st["history"].append({"ts": int(time.time()), "eq": snap["equity"],
                          "upnl": snap["upnl"], "level": level})

    if findings and not args.quiet:
        to_send = should_alert(st, level, findings)
        if to_send:
            try:
                from utils.notifier import Notifier
                Notifier().send_email(
                    f"[量化] 风险告警 ({'严重' if level=='critical' else '关注'})",
                    format_email(snap, to_send))
                log(f"  已发送 {len(to_send)} 条告警邮件")
            except Exception as e:
                log(f"  邮件发送异常: {e}")

    save_state(st)


if __name__ == "__main__":
    main()
