#!/usr/bin/env python3
"""Read-only commercial readiness risk report for OKX quant.

This script reads local SQLite/JSON/CSV analytics only. It does not call OKX,
place/cancel orders, edit config, or restart services.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB = DATA / "trading.db"
STATUS = DATA / "strategies_status.json"
WINDOWS_HOURS = (24, 72, 168)


def _float(v: Any, default: float = 0.0) -> float:
    try:
        if v in (None, ""):
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ts_iso(ts_ms: int | float | None) -> str:
    if not ts_ms:
        return ""
    try:
        return dt.datetime.fromtimestamp(float(ts_ms) / 1000).isoformat(timespec="seconds")
    except Exception:
        return ""


def _max_drawdown(values: list[float]) -> tuple[float, float]:
    peak = None
    worst_abs = 0.0
    worst_pct = 0.0
    for value in values:
        if peak is None or value > peak:
            peak = value
        if peak and value < peak:
            dd_abs = peak - value
            dd_pct = dd_abs / peak * 100 if peak else 0.0
            if dd_abs > worst_abs:
                worst_abs = dd_abs
                worst_pct = dd_pct
    return round(worst_abs, 4), round(worst_pct, 4)


def _db() -> sqlite3.Connection | None:
    if not DB.exists():
        return None
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def equity_windows(now_ms: int) -> dict[str, dict[str, Any]]:
    con = _db()
    if not con:
        return {}
    out: dict[str, dict[str, Any]] = {}
    cur = con.cursor()
    for h in WINDOWS_HOURS:
        cutoff = now_ms - h * 3600 * 1000
        rows = [dict(r) for r in cur.execute(
            "select ts,equity,usdt from equity where ts>=? order by ts asc", (cutoff,)
        )]
        if not rows:
            out[f"{h}h"] = {"samples": 0}
            continue
        values = [_float(r.get("equity")) for r in rows]
        dd_abs, dd_pct = _max_drawdown(values)
        start, end = rows[0], rows[-1]
        start_eq = _float(start["equity"])
        end_eq = _float(end["equity"])
        out[f"{h}h"] = {
            "samples": len(rows),
            "start_ts": start["ts"], "end_ts": end["ts"],
            "start_time": _ts_iso(start["ts"]), "end_time": _ts_iso(end["ts"]),
            "start_equity": round(start_eq, 4),
            "end_equity": round(end_eq, 4),
            "delta_equity": round(end_eq - start_eq, 4),
            "delta_pct": round((end_eq - start_eq) / start_eq * 100, 4) if start_eq else 0.0,
            "min_equity": round(min(values), 4),
            "max_equity": round(max(values), 4),
            "max_drawdown_abs": dd_abs,
            "max_drawdown_pct": dd_pct,
            "start_usdt": round(_float(start.get("usdt")), 4),
            "end_usdt": round(_float(end.get("usdt")), 4),
        }
    con.close()
    return out


def sqlite_realized(now_ms: int) -> dict[str, dict[str, Any]]:
    con = _db()
    if not con:
        return {}
    cur = con.cursor()
    out: dict[str, dict[str, Any]] = {}
    for h in WINDOWS_HOURS:
        cutoff = now_ms - h * 3600 * 1000
        rows = cur.execute(
            "select symbol, side, count(*) n, sum(pnl) gross, sum(fee) fee, sum(pnl-fee) net "
            "from trades where ts>=? group by symbol, side", (cutoff,)
        ).fetchall()
        total: dict[str, Any] = {"trades": 0, "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0, "by_symbol_side": []}
        for r in rows:
            item = {
                "symbol": r["symbol"], "side": r["side"], "trades": int(r["n"] or 0),
                "gross_pnl": round(_float(r["gross"]), 6),
                "fees": round(_float(r["fee"]), 6),
                "net_pnl": round(_float(r["net"]), 6),
            }
            total["by_symbol_side"].append(item)
            total["trades"] += item["trades"]
            total["gross_pnl"] += item["gross_pnl"]
            total["fees"] += item["fees"]
            total["net_pnl"] += item["net_pnl"]
        for k in ("gross_pnl", "fees", "net_pnl"):
            total[k] = round(total[k], 6)
        out[f"{h}h"] = total
    con.close()
    return out


def csv_sidecar_realized(now_ms: int) -> dict[str, Any]:
    out: dict[str, Any] = {f"{h}h": {"trades": 0, "gross_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0, "files": []} for h in WINDOWS_HOURS}
    for path in sorted(DATA.glob("pnl_records*.csv")):
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
        except Exception:
            continue
        for h in WINDOWS_HOURS:
            cutoff_s = now_ms / 1000 - h * 3600
            agg = defaultdict(float)
            n = 0
            for row in rows:
                row_ts = 0.0
                raw_time = (row.get("time") or row.get("ts") or "").strip()
                if raw_time:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            row_ts = dt.datetime.strptime(raw_time, fmt).timestamp(); break
                        except Exception:
                            pass
                    if not row_ts:
                        row_ts = _float(raw_time)
                        if row_ts > 10_000_000_000:
                            row_ts /= 1000
                if row_ts and row_ts < cutoff_s:
                    continue
                gross = _float(row.get("gross"), _float(row.get("pnl"), _float(row.get("net"))))
                fee = _float(row.get("fee"))
                net = _float(row.get("net"), gross - fee)
                agg["gross_pnl"] += gross; agg["fees"] += fee; agg["net_pnl"] += net; n += 1
            if n:
                item = {"file": path.name, "trades": n, "gross_pnl": round(agg["gross_pnl"], 6), "fees": round(agg["fees"], 6), "net_pnl": round(agg["net_pnl"], 6)}
                out[f"{h}h"]["files"].append(item)
                out[f"{h}h"]["trades"] += n
                out[f"{h}h"]["gross_pnl"] += item["gross_pnl"]
                out[f"{h}h"]["fees"] += item["fees"]
                out[f"{h}h"]["net_pnl"] += item["net_pnl"]
    for win in out.values():
        for k in ("gross_pnl", "fees", "net_pnl"):
            win[k] = round(win[k], 6)
    return out


def historical_strategy_extremes(now_ms: int) -> dict[str, Any]:
    con = _db()
    if not con:
        return {}
    cur = con.cursor()
    cutoff = now_ms - max(WINDOWS_HOURS) * 3600 * 1000
    rows = cur.execute("select ts,strategy,state from strategy_status where ts>=? order by ts asc", (cutoff,)).fetchall()
    agg: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            state = json.loads(r["state"] or "{}")
        except Exception:
            state = {}
        mon = state.get("monitor") if isinstance(state.get("monitor"), dict) else {}
        name = str(r["strategy"] or state.get("symbol") or "unknown")
        item = agg.setdefault(name, {"samples": 0, "max_single_side_skew_pct": 0.0, "max_margin_load_pct": 0.0, "max_unrealized_loss": 0.0})
        item["samples"] += 1
        item["max_single_side_skew_pct"] = max(item["max_single_side_skew_pct"], abs(_float(mon.get("imbalance_pct"))))
        item["max_margin_load_pct"] = max(item["max_margin_load_pct"], _float(mon.get("margin_load_pct")))
        item["max_unrealized_loss"] = min(item["max_unrealized_loss"], _float(mon.get("total_unrealized_pnl")))
    con.close()
    for item in agg.values():
        item["max_single_side_skew_pct"] = round(item["max_single_side_skew_pct"], 4)
        item["max_margin_load_pct"] = round(item["max_margin_load_pct"], 4)
        item["max_unrealized_loss"] = round(item["max_unrealized_loss"], 4)
    return agg


def current_strategies() -> dict[str, Any]:
    status = _safe_json(STATUS) or {}
    out: dict[str, Any] = {}
    for name, state in (status.get("strategies") or {}).items():
        if not isinstance(state, dict):
            continue
        mon = state.get("monitor") if isinstance(state.get("monitor"), dict) else {}
        skew = abs(_float(mon.get("imbalance_pct")))
        margin = _float(mon.get("margin_load_pct"))
        upnl = _float(mon.get("total_unrealized_pnl"))
        out[name] = {
            "symbol": state.get("symbol") or name,
            "running": bool(state.get("running", status.get("running", False))),
            "halted": bool(state.get("halted", False)),
            "pnl_reported": round(_float(state.get("pnl")), 6),
            "fees_reported": round(_float(state.get("total_fees")), 6),
            "long_qty": _float(state.get("long_qty"), _float(mon.get("long_qty"))),
            "short_qty": _float(state.get("short_qty"), _float(mon.get("short_qty"))),
            "long_notional": round(_float(mon.get("long_notional")), 4),
            "short_notional": round(_float(mon.get("short_notional")), 4),
            "net_exposure_usdt": round(_float(mon.get("net_exposure_usdt")), 4),
            "single_side_skew_pct": round(skew, 4),
            "dominant_side": mon.get("dominant_side") or "",
            "margin_load_pct": round(margin, 4),
            "unrealized_pnl": round(upnl, 6),
            "tp_order_count": int(_float(mon.get("tp_order_count"))),
            "grid_long_block_reason": mon.get("grid_long_block_reason") or mon.get("long_add_block_reason") or "",
            "grid_short_block_reason": mon.get("grid_short_block_reason") or mon.get("short_add_block_reason") or "",
            "order_tracker": mon.get("order_tracker") if isinstance(mon.get("order_tracker"), dict) else {},
        }
    return out


def readiness_findings(current: dict[str, Any], equity: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    e24 = equity.get("24h") or {}
    if _float(e24.get("delta_equity")) < 0:
        findings.append({"level": "warn", "item": "24h_equity", "message": f"近24h权益下降 {e24.get('delta_equity')}，不能只看已实现PnL。"})
    for name, s in current.items():
        if _float(s.get("single_side_skew_pct")) >= 70:
            findings.append({"level": "warn", "item": name, "message": f"{s.get('symbol')} 单边偏离 {s.get('single_side_skew_pct')}%，商用前需降低或解释。"})
        if _float(s.get("unrealized_pnl")) < -25:
            findings.append({"level": "warn", "item": name, "message": f"{s.get('symbol')} 当前未实现PnL {s.get('unrealized_pnl')}，需要纳入客户展示和风控。"})
        if not s.get("running") or s.get("halted"):
            findings.append({"level": "critical", "item": name, "message": f"{s.get('symbol')} 未正常运行或已halt。"})
    if not findings:
        findings.append({"level": "info", "item": "runtime", "message": "当前未发现硬性停止项，但仍需持续观察真实权益曲线。"})
    return findings


def build_report() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    status_age = round(time.time() - STATUS.stat().st_mtime, 2) if STATUS.exists() else None
    equity = equity_windows(now_ms)
    current = current_strategies()
    return {
        "code": 0,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "read_only": True,
        "sources": {
            "database": str(DB.relative_to(ROOT)),
            "status_file": str(STATUS.relative_to(ROOT)),
            "status_age_sec": status_age,
            "coverage_note": "SQLite strategy_status history may cover only the primary synced profile; current snapshot covers all live profiles.",
        },
        "equity_windows": equity,
        "realized_pnl_sqlite": sqlite_realized(now_ms),
        "realized_pnl_csv_sidecars": csv_sidecar_realized(now_ms),
        "current_strategies": current,
        "historical_strategy_extremes_7d": historical_strategy_extremes(now_ms),
        "readiness_findings": readiness_findings(current, equity),
        "commercial_gates": {
            "strategy_stats": "partial_ready_read_only",
            "https_reverse_proxy_no_url_token": "pending",
            "encrypted_customer_keys_precheck_forced_paper": "partial_existing_needs_hardening",
            "workspace_isolation": "pending",
            "circuit_breaker_risk_report_manual_takeover": "risk_report_started",
            "pnl_breakdown": "started",
        },
    }


def print_text(rep: dict[str, Any]) -> None:
    print(f"OKX commercial risk report @ {rep['generated_at']} read_only={rep['read_only']}")
    print(f"status_age={rep['sources'].get('status_age_sec')}s")
    print("Equity:")
    for win, e in rep.get("equity_windows", {}).items():
        print(f"  {win}: {e.get('start_equity')} -> {e.get('end_equity')} delta={e.get('delta_equity')} drawdown={e.get('max_drawdown_pct')}% samples={e.get('samples')}")
    print("Current strategies:")
    for name, s in rep.get("current_strategies", {}).items():
        print(f"  {name} {s.get('symbol')}: running={s.get('running')} skew={s.get('single_side_skew_pct')}% margin={s.get('margin_load_pct')}% upnl={s.get('unrealized_pnl')} net={s.get('net_exposure_usdt')} tp={s.get('tp_order_count')}")
    print("Readiness findings:")
    for f in rep.get("readiness_findings", []):
        print(f"  [{f.get('level')}] {f.get('item')}: {f.get('message')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = ap.parse_args()
    rep = build_report()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
