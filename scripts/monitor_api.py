#!/usr/bin/env python3
"""本机监控面板专用 — 只读数据端点。

独立运行在 127.0.0.1:8797，不复用主 Web 服务的鉴权链路。
所有数据从交易所 API 实时拉取，绝不写入、绝不下单。

启动：
    ./venv/bin/python monitor_api.py
监听：127.0.0.1:8797
端点：
    GET /api/snapshot   账户全景快照（权益/浮亏/持仓/对冲/告警）
    GET /api/health     健康检查
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/opt/okx_quant")
sys.path.insert(0, "/opt/okx_quant/web")
os.chdir("/opt/okx_quant")

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

app = FastAPI(title="OKX Monitor API", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["GET"], allow_headers=["*"],
)

# 缓存：避免高频请求打爆交易所限频
_CACHE = {"ts": 0.0, "data": None}
CACHE_TTL = 5.0

# 阈值（与服务器端 risk_sentinel.py 保持一致）
THRESHOLDS = {
    "upnl_warn": -180.0, "upnl_crit": -250.0,
    "upnl_pct_warn": 18.0, "upnl_pct_crit": 25.0,
    "avail_pct_warn": 40.0, "avail_pct_crit": 25.0,
    "conc_warn": 60.0, "conc_crit": 75.0, "conc_min_upnl": -80.0,
    "dd_warn": 5.0, "dd_crit": 8.0, "baseline_equity": 1000.0,
    "hedge_degrade_pct": 80.0, "hedge_net_usd": 300.0,
}

# ---- 资金费率（账单 type=8 精确值；只读，5 分钟刷新）----
CYCLE_START_MS = int(datetime(2026, 9, 26).timestamp() * 1000)
_FUND = {"ts": 0.0, "data": None, "err": None}
FUND_TTL = 300.0
FUND_MAX_PAGES = 12


def collect_funding(client, syms):
    """精确资金费：账户账单 type=8（funding fee）+ 各品种当前费率。"""
    today0 = datetime.now().replace(hour=0, minute=0, second=0,
                                    microsecond=0).timestamp() * 1000
    rows, complete, after = [], False, ""
    for _ in range(FUND_MAX_PAGES):
        resp = client._call(client._account.get_account_bills,
                            instType="SWAP", type="8",
                            after=after, limit="100")
        if str(resp.get("code")) != "0":
            raise RuntimeError(f"bills failed: {resp.get('code')} "
                               f"{resp.get('msg', '')}")
        page = resp.get("data") or []
        if not page:
            complete = True
            break
        rows.extend(page)
        oldest = min(int(float(x.get("ts") or 0)) for x in page)
        if oldest < CYCLE_START_MS:
            complete = True
            break
        nxt = str(page[-1].get("billId") or "")
        if not nxt or nxt == after:
            break
        after = nxt

    cycle = today = 0.0
    by_sym = {}
    for r in rows:
        ts = int(float(r.get("ts") or 0))
        if ts < CYCLE_START_MS:
            continue
        chg = float(r.get("balChg") or 0)
        cycle += chg
        sym = r.get("instId") or "?"
        by_sym[sym] = by_sym.get(sym, 0.0) + chg
        if ts >= today0:
            today += chg

    rates = {}
    for s in syms:
        try:
            rates[s] = client.get_funding_rate(s)
        except Exception:
            rates[s] = None
    return {
        "today": round(today, 4), "cycle": round(cycle, 4),
        "since": "2026-09-26",
        "by_sym": {k: round(v, 4) for k, v in by_sym.items()},
        "rates_8h": rates, "complete": complete,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_client():
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    from config_manager import load_api_password_secure
    cm = CredentialManager({"exchange": {"okx": {"key_file": "data/keys.enc"}}})
    cm.set_password(load_api_password_secure())
    return OKXClient(cm.load())


def collect():
    """从交易所实时采集账户全景。"""
    client = get_client()
    raw = client.get_account_balance_raw()
    item = (raw.get("data") or [{}])[0]

    eq = float(item.get("totalEq") or 0)
    avail = frozen = ccy_eq = 0.0
    for d in item.get("details") or []:
        if d.get("ccy") == "USDT":
            avail = float(d.get("availBal") or 0)
            frozen = float(d.get("frozenBal") or 0)
            ccy_eq = float(d.get("eq") or 0)

    positions = []
    bysym = {}
    g_upnl = g_margin = g_notional = 0.0
    for p in client.get_positions():
        qty = float(p.get("pos") or 0)
        if abs(qty) < 1e-12:
            continue
        sym = p.get("instId")
        side = p.get("posSide")
        mark = float(p.get("markPx") or 0)
        upl = float(p.get("upl") or 0)
        margin = float(p.get("margin") or 0)
        # OKX returns a ctVal-adjusted USD notional directly. Falling back to
        # qty * mark is only correct when ctVal == 1 (CRV/FARTCOIN), so the
        # multiplier must be applied explicitly for contracts like ETH (0.1).
        notional = abs(float(p.get("notionalUsd") or 0))
        if notional <= 0:
            ct_val = float(p.get("ctVal") or 1.0)
            notional = abs(qty) * ct_val * mark
        g_upnl += upl
        g_margin += margin
        g_notional += notional

        e = bysym.setdefault(sym, {"long_usd": 0.0, "short_usd": 0.0,
                                   "upl": 0.0})
        if side == "long":
            e["long_usd"] += notional
        else:
            e["short_usd"] += notional
        e["upl"] += upl

        positions.append({
            "sym": sym, "side": side, "qty": qty,
            "avg_px": float(p.get("avgPx") or 0), "mark_px": mark,
            "upl": upl, "margin": margin, "notional": notional,
            "lever": float(p.get("lever") or 0),
            "liq_px": float(p.get("liqPx") or 0) if p.get("liqPx") else 0.0,
        })

    # 对冲结构
    hedge = []
    for sym, e in bysym.items():
        gross = e["long_usd"] + e["short_usd"]
        net = e["long_usd"] - e["short_usd"]
        ratio = min(e["long_usd"], e["short_usd"]) * 2 / gross * 100 \
            if gross else 0
        hedge.append({
            "sym": sym, "gross_usd": gross, "net_usd": net,
            "hedge_ratio": ratio, "upl": e["upl"],
        })
    hedge.sort(key=lambda x: -abs(x["net_usd"]))

    # 资金费（5 分钟缓存；失败时保留上次结果并在 error 字段注明）
    syms = sorted(bysym.keys())
    now_t = time.time()
    if _FUND["data"] is None or now_t - _FUND["ts"] >= FUND_TTL:
        try:
            _FUND["data"] = collect_funding(client, syms)
            _FUND["ts"] = now_t
            _FUND["err"] = None
        except Exception as e:
            _FUND["err"] = f"{type(e).__name__}: {e}"
    funding = dict(_FUND["data"] or {"today": 0.0, "cycle": 0.0,
                                     "rates_8h": {}, "by_sym": {}})
    if _FUND["err"]:
        funding["error"] = _FUND["err"]
    for h in hedge:
        h["fund_rate_8h"] = (funding.get("rates_8h") or {}).get(h["sym"])

    # 挂单
    try:
        live = [o for o in (client.get_order_list(state="live") or [])
                if str(o.get("state", "")).lower() == "live"]
    except Exception:
        live = []

    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "epoch": int(time.time()),
        "equity": eq,
        "avail": avail,
        "frozen": frozen,
        "ccy_eq": ccy_eq,
        "upnl": g_upnl,
        "margin": g_margin,
        "notional": g_notional,
        "net_exposure": sum(h["net_usd"] for h in hedge),
        "positions": positions,
        "hedge": hedge,
        "funding": funding,
        "orders_live": len(live),
    }


def evaluate(snap):
    """按阈值评估，返回告警列表与总体级别。"""
    T = THRESHOLDS
    alerts = []
    eq = snap["equity"] or 1.0
    upnl = snap["upnl"]
    avail = snap["avail"]

    def add(lv, title, detail):
        alerts.append({"level": lv, "title": title, "detail": detail})

    if upnl <= T["upnl_crit"]:
        add("critical", "浮亏触及严重线",
            f"未实现盈亏 {upnl:+.2f} U（严重线 {T['upnl_crit']:.0f}）")
    elif upnl <= T["upnl_warn"]:
        add("warning", "浮亏触及关注线",
            f"未实现盈亏 {upnl:+.2f} U（关注线 {T['upnl_warn']:.0f}）")

    if upnl < 0:
        pctl = abs(upnl) / eq * 100
        if pctl >= T["upnl_pct_crit"]:
            add("critical", "浮亏占比过高",
                f"占权益 {pctl:.1f}%（严重线 {T['upnl_pct_crit']:.0f}%）")
        elif pctl >= T["upnl_pct_warn"]:
            add("warning", "浮亏占比偏高",
                f"占权益 {pctl:.1f}%（关注线 {T['upnl_pct_warn']:.0f}%）")

    apct = avail / eq * 100
    if apct <= T["avail_pct_crit"]:
        add("critical", "可用保证金不足",
            f"可用 {avail:.2f} U，占权益 {apct:.1f}%")
    elif apct <= T["avail_pct_warn"]:
        add("warning", "可用保证金偏低",
            f"可用 {avail:.2f} U，占权益 {apct:.1f}%")

    if upnl <= T["conc_min_upnl"]:
        losses = sorted((p for p in snap["positions"] if p["upl"] < 0),
                        key=lambda x: x["upl"])
        if losses:
            w = losses[0]
            conc = abs(w["upl"]) / abs(upnl) * 100
            if conc >= T["conc_crit"]:
                add("critical", "单品种风险集中",
                    f"{w['sym']} 浮亏 {w['upl']:+.2f} U，占 {conc:.1f}%")
            elif conc >= T["conc_warn"]:
                add("warning", "单品种亏损集中",
                    f"{w['sym']} 浮亏 {w['upl']:+.2f} U，占 {conc:.1f}%")

    peak = max(T["baseline_equity"], eq)
    dd = (peak - eq) / peak * 100
    if dd >= T["dd_crit"]:
        add("critical", "权益回撤超限", f"从 {peak:.2f} 回撤 {dd:.2f}%")
    elif dd >= T["dd_warn"]:
        add("warning", "权益回撤偏大", f"从 {peak:.2f} 回撤 {dd:.2f}%")

    # 对冲退化 + 净敞口放大 → 资金费按单边计费，消耗加速
    for h in snap.get("hedge", []):
        if h["hedge_ratio"] < T["hedge_degrade_pct"] \
                and abs(h["net_usd"]) > T["hedge_net_usd"]:
            r8 = h.get("fund_rate_8h") or 0.0
            burn = abs(h["net_usd"]) * abs(r8) * 3
            add("warning", "对冲退化：资金费消耗加速",
                f"{h['sym']} 对冲 {h['hedge_ratio']:.0f}%，净敞口 "
                f"{h['net_usd']:+.0f} USD，当前费率下约 "
                f"{burn:.2f} U/天（净额口径）")
            break

    order = {"ok": 0, "warning": 1, "critical": 2}
    level = "ok"
    for a in alerts:
        if order[a["level"]] > order[level]:
            level = a["level"]
    return level, alerts


@app.get("/api/health")
def health():
    return {"ok": True, "service": "okx-monitor-api",
            "ts": datetime.now().isoformat()}


@app.get("/api/snapshot")
def snapshot():
    now = time.time()
    if _CACHE["data"] and now - _CACHE["ts"] < CACHE_TTL:
        return JSONResponse(_CACHE["data"])
    try:
        snap = collect()
        level, alerts = evaluate(snap)
        snap["level"] = level
        snap["alerts"] = alerts
        snap["stale"] = False
        _CACHE["data"] = snap
        _CACHE["ts"] = now
        return JSONResponse(snap)
    except Exception as e:
        if _CACHE["data"]:
            d = dict(_CACHE["data"])
            d["stale"] = True
            d["error"] = f"{type(e).__name__}: {e}"
            return JSONResponse(d)
        return JSONResponse({"error": f"{type(e).__name__}: {e}",
                             "stale": True}, status_code=503)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8797, log_level="warning")
