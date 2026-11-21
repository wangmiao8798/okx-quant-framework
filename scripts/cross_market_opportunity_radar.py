#!/usr/bin/env python3
"""Public, read-only BTC/ETH-to-US-equities opportunity radar.

Observation only: no credentials, private endpoints, orders, positions or trading actions.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/cross_market_assets.yaml"
DEFAULT_DB = ROOT / "data/cross_market_opportunity.db"
DEFAULT_REPORT = ROOT / "data/cross_market_opportunity_report.json"
NASDAQ = "https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset_class}"
OKX = "https://www.okx.com/api/v5/market/ticker?instId={symbol}"
UA = "Mozilla/5.0 okx-quant-cross-market-radar/1.0"
ETF = {"IBIT", "ETHA", "QQQ"}
OPEN_STATUSES = {"market open", "open", "regular market"}


def us_regular_session(at: datetime | None = None) -> bool:
    local = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace("$", "").replace("%", "").replace(",", ""))
    except (TypeError, ValueError):
        return default


def get_json(url: str, timeout: int = 20) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_nasdaq_quote(payload: dict[str, Any], fetched_at_ms: int | None = None) -> dict[str, Any]:
    data = payload.get("data") or {}
    primary, secondary = data.get("primaryData") or {}, data.get("secondaryData") or {}
    price = number(primary.get("lastSalePrice"))
    change = number(secondary.get("percentageChange"), number(primary.get("percentageChange")))
    ts = fetched_at_ms or int(time.time() * 1000)
    return {
        "ok": price > 0,
        "symbol": data.get("symbol") or "",
        "price": price,
        "change_pct": change,
        "market_status": data.get("marketStatus") or "unknown",
        "fetched_at_ms": ts,
        "age_seconds": 0,
        "source": "nasdaq_public",
        **({} if price > 0 else {"error": "missing Nasdaq primary price"}),
    }


def parse_okx_ticker(ticker: dict[str, Any], fetched_at_ms: int | None = None) -> dict[str, Any]:
    price, opened = number(ticker.get("last")), number(ticker.get("open24h"))
    ts = fetched_at_ms or int(time.time() * 1000)
    quote_ts = int(number(ticker.get("ts")))
    timestamp_ok = quote_ts > 0
    return {
        "ok": price > 0 and opened > 0 and timestamp_ok,
        "symbol": ticker.get("instId") or "",
        "price": price,
        "change_pct": (price / opened - 1) * 100 if price > 0 and opened > 0 else 0.0,
        "quote_ts_ms": quote_ts,
        "fetched_at_ms": ts,
        "age_seconds": max(0.0, (ts - quote_ts) / 1000) if timestamp_ok else float("inf"),
        "source": "okx_public",
        **({} if timestamp_ok else {"error": "missing OKX quote timestamp"}),
    }


def fetch_nasdaq(symbol: str) -> dict[str, Any]:
    now = int(time.time() * 1000)
    try:
        kind = "etf" if symbol in ETF else "stocks"
        return parse_nasdaq_quote(get_json(NASDAQ.format(symbol=urllib.parse.quote(symbol), asset_class=kind)), now)
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "price": 0.0, "change_pct": 0.0, "age_seconds": 0, "fetched_at_ms": now, "source": "nasdaq_public", "error": f"{type(exc).__name__}: {exc}"}


def fetch_okx(symbol: str) -> dict[str, Any]:
    now = int(time.time() * 1000)
    try:
        row = (get_json(OKX.format(symbol=urllib.parse.quote(symbol))).get("data") or [{}])[0]
        result = parse_okx_ticker(row, now)
        result["symbol"] = symbol
        return result
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "price": 0.0, "change_pct": 0.0, "age_seconds": 0, "fetched_at_ms": now, "source": "okx_public", "error": f"{type(exc).__name__}: {exc}"}


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if cfg.get("public_read_only") is not True or cfg.get("advisory_only") is not True:
        raise ValueError("public_read_only=true and advisory_only=true are mandatory")
    if not cfg.get("themes"):
        raise ValueError("at least one theme is required")
    return cfg


def evaluate_theme(name: str, cfg: dict[str, Any], quotes: dict[str, dict[str, Any]], captured_at: str) -> dict[str, Any]:
    theme = cfg["themes"][name]
    crypto_symbol, benchmark_symbol = theme["crypto"], theme["benchmark"]
    max_age = int(cfg.get("quality", {}).get("max_age_seconds", 300))
    minimum = int(cfg.get("quality", {}).get("min_equities", 2))
    threshold = number(cfg.get("thresholds", {}).get("lead_pct"), 2.0)
    reasons: list[str] = []
    crypto, benchmark = quotes.get(crypto_symbol), quotes.get(benchmark_symbol)
    if not crypto or not crypto.get("ok"): reasons.append("missing_crypto")
    elif number(crypto.get("age_seconds")) > max_age: reasons.append("stale_crypto")
    if not benchmark or not benchmark.get("ok"): reasons.append("missing_benchmark")
    elif number(benchmark.get("age_seconds")) > max_age: reasons.append("stale_benchmark")
    equity_market_open = str((benchmark or {}).get("market_status", "")).lower() in {"market open", "open", "regular market"}
    if not equity_market_open:
        reasons.append("equity_market_closed")
    valid_equities = [quotes[s] for s in theme["equities"] if quotes.get(s, {}).get("ok") and number(quotes[s].get("age_seconds")) <= max_age]
    if len(valid_equities) < minimum: reasons.append("insufficient_equities")

    aligned = bool(crypto and benchmark and "window_change_pct" in crypto and "window_change_pct" in benchmark and all("window_change_pct" in q for q in valid_equities))
    if not aligned:
        reasons.append("unaligned_return_window")
    crypto_change = number((crypto or {}).get("window_change_pct")) if aligned else number((crypto or {}).get("change_pct"))
    benchmark_change = number((benchmark or {}).get("window_change_pct")) if aligned else number((benchmark or {}).get("change_pct"))
    change_key = "window_change_pct" if aligned else "change_pct"
    basket_change = sum(number(q.get(change_key)) for q in valid_equities) / len(valid_equities) if valid_equities else 0.0
    excess = basket_change - benchmark_change
    divergence = crypto_change - excess
    signal = "neutral"
    if not reasons:
        if divergence >= threshold: signal = "crypto_leads"
        elif divergence <= -threshold: signal = "equities_lead"
        else: reasons.append("inside_band")
    reasons.append("advisory_only")
    return {
        "theme": name, "captured_at": captured_at, "crypto": crypto_symbol,
        "equities": list(theme["equities"]), "valid_equities": len(valid_equities),
        "benchmark": benchmark_symbol, "crypto_change_24h_pct": crypto_change,
        "equity_basket_change_pct": basket_change, "benchmark_change_pct": benchmark_change,
        "equity_excess_pct": excess, "divergence_pct": divergence, "signal": signal,
        "data_quality": "ok" if reasons in (["advisory_only"], ["inside_band", "advisory_only"]) else "bad",
        "reasons": reasons, "advisory_only": True,
    }


def collect(cfg: dict[str, Any], at: datetime | None = None) -> dict[str, Any]:
    now = at or datetime.now(timezone.utc)
    captured = now.isoformat(timespec="seconds")
    if not us_regular_session(now):
        return {"captured_at": captured, "skipped": True, "skip_reason": "outside_us_regular_session"}
    equities = sorted({s for t in cfg["themes"].values() for s in [*t["equities"], t["benchmark"]]})
    cryptos = sorted({t["crypto"] for t in cfg["themes"].values()})
    benchmarks = sorted({t["benchmark"] for t in cfg["themes"].values()})
    quotes = {s: fetch_nasdaq(s) for s in benchmarks}
    if any(str(quotes[s].get("market_status", "")).lower() not in OPEN_STATUSES for s in benchmarks):
        return {"captured_at": captured, "skipped": True, "skip_reason": "us_reference_market_closed"}
    quotes.update({s: fetch_nasdaq(s) for s in equities if s not in quotes})
    quotes.update({s: fetch_okx(s) for s in cryptos})
    themes = [evaluate_theme(name, cfg, quotes, captured) for name in cfg["themes"]]
    return {"captured_at": captured, "source": "public_read_only_cross_market", "advisory_only": True,
            "note": "Observation only; no profitability claim or trading action.", "quotes": list(quotes.values()), "themes": themes}


def ensure_schema(conn: sqlite3.Connection) -> None:
    expected = {
        "normalized_quotes": ["batch_id", "symbol", "source", "price", "change_pct", "age_seconds", "ok", "payload_json"],
        "theme_snapshots": ["batch_id", "theme", "crypto_change", "equity_change", "benchmark_change", "equity_excess", "divergence", "data_quality", "payload_json"],
        "shadow_signals": ["batch_id", "theme", "signal", "advisory_only", "reasons_json", "payload_json"],
    }
    existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, columns in expected.items():
        if table in existing:
            actual = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            if actual != columns:
                raise RuntimeError(f"incompatible schema for {table}: {actual}")
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS normalized_quotes(batch_id TEXT,symbol TEXT,source TEXT,price REAL,change_pct REAL,age_seconds REAL,ok INTEGER,payload_json TEXT,PRIMARY KEY(batch_id,symbol));
    CREATE TABLE IF NOT EXISTS theme_snapshots(batch_id TEXT,theme TEXT,crypto_change REAL,equity_change REAL,benchmark_change REAL,equity_excess REAL,divergence REAL,data_quality TEXT,payload_json TEXT,PRIMARY KEY(batch_id,theme));
    CREATE TABLE IF NOT EXISTS shadow_signals(batch_id TEXT,theme TEXT,signal TEXT,advisory_only INTEGER,reasons_json TEXT,payload_json TEXT,PRIMARY KEY(batch_id,theme));
    """)


def save_results(db: Path, batch_id: str, quotes: list[dict[str, Any]], themes: list[dict[str, Any]]) -> None:
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db, timeout=30) as conn:
        ensure_schema(conn)
        for q in quotes:
            conn.execute("INSERT OR REPLACE INTO normalized_quotes(batch_id,symbol,source,price,change_pct,age_seconds,ok,payload_json) VALUES(?,?,?,?,?,?,?,?)", (batch_id, q["symbol"], q.get("source", ""), q.get("price"), q.get("change_pct"), q.get("age_seconds"), bool(q.get("ok", True)), json.dumps(q, ensure_ascii=False, sort_keys=True)))
        for t in themes:
            payload = json.dumps(t, ensure_ascii=False, sort_keys=True)
            conn.execute("INSERT OR REPLACE INTO theme_snapshots(batch_id,theme,crypto_change,equity_change,benchmark_change,equity_excess,divergence,data_quality,payload_json) VALUES(?,?,?,?,?,?,?,?,?)", (batch_id, t["theme"], t.get("crypto_change_24h_pct"), t.get("equity_basket_change_pct"), t.get("benchmark_change_pct"), t.get("equity_excess_pct"), t.get("divergence_pct"), t.get("data_quality", "unknown"), payload))
            conn.execute("INSERT OR REPLACE INTO shadow_signals(batch_id,theme,signal,advisory_only,reasons_json,payload_json) VALUES(?,?,?,?,?,?)", (batch_id, t["theme"], t["signal"], 1, json.dumps(t.get("reasons", [])), payload))
        conn.commit()


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=Path(path).name + ".", dir=Path(path).parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(payload, out, ensure_ascii=False, indent=2, sort_keys=True); out.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def compact_summary(themes: list[dict[str, Any]]) -> str:
    return "advisory_only | " + " | ".join(f"{t['theme']}={t['signal']}({number(t.get('divergence_pct')):+.2f}%,q={t.get('data_quality','?')})" for t in themes)


def main() -> int:
    p = argparse.ArgumentParser(description="Read-only cross-market opportunity radar")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG); p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT); p.add_argument("--json", action="store_true")
    args = p.parse_args(); report = collect(load_config(args.config))
    if report.get("skipped"):
        print(f"advisory_only | skipped={report['skip_reason']}")
        return 0
    save_results(args.db, report["captured_at"], report["quotes"], report["themes"]); write_atomic(args.report, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else compact_summary(report["themes"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
