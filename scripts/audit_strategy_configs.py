#!/usr/bin/env python3
"""Score OKX strategy configs for customer live admission.

Read-only: loads local config YAML files and OKX public market data, then prints
risk/admission suggestions. It does not start strategies, place orders, or write
runtime state unless --json-output is provided.
"""
import argparse
import glob
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

PROJECT = Path(__file__).resolve().parents[1]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.okx.com",
    "Referer": "https://www.okx.com/",
}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def okx_market(symbol: str) -> dict:
    ticker_url = "https://www.okx.com/api/v5/market/ticker?" + urllib.parse.urlencode({"instId": symbol})
    inst_url = "https://www.okx.com/api/v5/public/instruments?" + urllib.parse.urlencode({"instType": "SWAP", "instId": symbol})
    ticker = fetch_json(ticker_url).get("data") or []
    inst = fetch_json(inst_url).get("data") or []
    if not ticker or not inst:
        return {"ok": False, "error": "market data missing"}
    t = ticker[0]
    i = inst[0]
    last = float(t.get("last") or 0)
    high = float(t.get("high24h") or 0)
    low = float(t.get("low24h") or 0)
    min_sz = float(i.get("minSz") or 0)
    lot_sz = float(i.get("lotSz") or 0)
    tick_sz = float(i.get("tickSz") or 0)
    ct_val = float(i.get("ctVal") or 0)
    ct_ccy = str(i.get("ctValCcy") or "").upper()
    contract_value = ct_val if ct_ccy == "USDT" else ct_val * last
    vol_range = ((high - low) / last) if last > 0 and high >= low else 0.0
    return {
        "ok": True,
        "last": last,
        "high24h": high,
        "low24h": low,
        "range24h_pct": vol_range * 100,
        "state": i.get("state", ""),
        "minSz": min_sz,
        "lotSz": lot_sz,
        "tickSz": tick_sz,
        "ctVal": ct_val,
        "ctValCcy": ct_ccy,
        "contract_value_usdt": contract_value,
        "min_notional_usdt": min_sz * contract_value,
    }


def config_paths(include_enabled: bool, include_standby: bool) -> list[Path]:
    paths = []
    if include_enabled:
        paths.extend([PROJECT / "config" / "config.yaml", PROJECT / "config" / "config_fartcoin.yaml"])
        paths.extend(Path(p) for p in glob.glob(str(PROJECT / "config" / "config_*.yaml")))
        # Production candidates are kept at the project root in this audit
        # workspace; include them explicitly, while leaving remote snapshots
        # out because they are historical evidence rather than deploy inputs.
        paths.extend(Path(p) for p in glob.glob(str(PROJECT / "config_*.production.yaml")))
    if include_standby:
        paths.extend(Path(p) for p in glob.glob(str(PROJECT / "config" / "standby" / "config_*.yaml")))
    seen = []
    for path in paths:
        if path.exists() and path not in seen:
            seen.append(path)
    return sorted(seen, key=lambda p: str(p))


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    fut = cfg.get("futures", {}) or {}
    profile = cfg.get("profile", {}) or {}
    return {"path": str(path.relative_to(PROJECT)), "profile": profile, "futures": fut}


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def score_config(item: dict, market: dict) -> dict:
    fut = item["futures"]
    symbol = str(fut.get("symbol") or "").upper()
    leverage = float(fut.get("leverage") or 1)
    base = float(fut.get("base_position_usdt") or 0)
    layers = int(float(fut.get("max_add_layers") or 0))
    add_step = float(fut.get("add_step") or 0)
    first_tp = float(fut.get("first_tp_ratio") or 0)
    max_margin = float(fut.get("max_margin_ratio") or 0)
    add_multiplier = float(fut.get("add_multiplier") or 1)
    issues = []
    suggestions = []
    score = 100

    if leverage > 5:
        issues.append(f"leverage_high:{leverage:g}x")
        score -= 18
    elif leverage < 2:
        suggestions.append("leverage is conservative")

    if layers > 6:
        issues.append(f"too_many_layers:{layers}")
        score -= 12
    if max_margin > 0.55:
        issues.append(f"max_margin_high:{max_margin:.2f}")
        score -= 14
    if base <= 0:
        issues.append("base_position_missing")
        score -= 20

    if market.get("ok"):
        range24 = float(market.get("range24h_pct") or 0)
        min_margin = float(market.get("min_notional_usdt") or 0) / max(leverage, 1)
        base_multiple = (base / min_margin) if min_margin > 0 else None
        if base_multiple is not None and base_multiple < 1.5:
            issues.append(f"base_too_close_to_min_contract:{base_multiple:.2f}x")
            score -= 18
        if range24 >= 12 and add_step < 0.006:
            issues.append(f"add_step_tight_for_high_vol:{add_step*100:.2f}% vs 24h {range24:.1f}%")
            score -= 16
        elif range24 >= 7 and add_step < 0.0035:
            issues.append(f"add_step_tight_for_vol:{add_step*100:.2f}% vs 24h {range24:.1f}%")
            score -= 10
        if first_tp <= add_step * 1.2:
            issues.append("tp_too_close_to_add_step")
            score -= 8
        if first_tp < 0.004:
            issues.append(f"tp_may_be_fee_sensitive:{first_tp*100:.2f}%")
            score -= 8
        # recommendations
        suggested_add = add_step
        if range24 >= 12:
            suggested_add = max(add_step, 0.006)
        elif range24 >= 7:
            suggested_add = max(add_step, 0.0035)
        elif range24 <= 3:
            suggested_add = max(add_step, 0.0015)
        suggested_tp = max(first_tp, suggested_add * 1.8, 0.005)
        suggestions.extend([
            f"suggest_add_step={suggested_add:.4f}",
            f"suggest_first_tp={suggested_tp:.4f}",
            f"min_margin_at_cfg_leverage={min_margin:.4f}",
        ])
    else:
        issues.append("market_data_unavailable")
        score -= 15

    if add_multiplier > 1.3:
        issues.append(f"add_multiplier_aggressive:{add_multiplier:.2f}")
        score -= 8

    score = int(clamp(score, 0, 100))
    if score >= 82 and not any(x.startswith("market_data") for x in issues):
        admission = "allow_live"
    elif score >= 68:
        admission = "paper_or_small_live"
    else:
        admission = "do_not_open_live"
    risk = "low" if score >= 85 else "medium" if score >= 70 else "high"
    return {
        "symbol": symbol,
        "path": item["path"],
        "score": score,
        "risk": risk,
        "admission": admission,
        "config": {
            "leverage": leverage,
            "base_position_usdt": base,
            "max_add_layers": layers,
            "add_step": add_step,
            "first_tp_ratio": first_tp,
            "max_margin_ratio": max_margin,
        },
        "market": market,
        "issues": issues,
        "suggestions": suggestions,
    }


def main():
    ap = argparse.ArgumentParser(description="Audit OKX strategy configs for live admission")
    ap.add_argument("--standby-only", action="store_true", help="audit only config/standby/*.yaml")
    ap.add_argument("--json-output", default="", help="optional path to write JSON report")
    args = ap.parse_args()
    paths = config_paths(include_enabled=not args.standby_only, include_standby=True)
    if args.standby_only:
        paths = [p for p in paths if "/standby/" in str(p).replace("\\", "/")]
    results = []
    for path in paths:
        item = load_config(path)
        symbol = str(item["futures"].get("symbol") or "").upper()
        try:
            market = okx_market(symbol)
        except Exception as exc:
            market = {"ok": False, "error": type(exc).__name__, "msg": str(exc)[:160]}
        results.append(score_config(item, market))
    print("symbol score risk admission issues suggestions")
    for r in results:
        print(f"{r['symbol']} {r['score']} {r['risk']} {r['admission']} issues={';'.join(r['issues']) or '-'} suggestions={';'.join(r['suggestions']) or '-'}")
    if args.json_output:
        out = Path(args.json_output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"json_output={out}")

if __name__ == "__main__":
    main()
