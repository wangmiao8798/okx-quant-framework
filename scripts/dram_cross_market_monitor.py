#!/usr/bin/env python3
"""Read-only cross-market monitor for Roundhill Memory ETF / OKX DRAM swap.

Sources:
- Nasdaq public quote endpoints: DRAM ETF and Micron (MU)
- Naver Finance public pages: Samsung Electronics and SK Hynix
- OKX public endpoints: ticker, index, mark price, funding and order book

The script never reads API keys and never places, changes, or cancels orders.
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "dram_cross_market.db"
DEFAULT_REPORT = ROOT / "data" / "dram_cross_market_report.json"
USER_AGENT = "Mozilla/5.0 okx-quant-dram-cross-market-monitor/1.0"
NASDAQ_URL = "https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset_class}"
NAVER_URL = "https://finance.naver.com/item/main.naver?code={code}"
OKX_BASE = "https://www.okx.com"
OPEN_STATUSES = {"market open", "open", "regular market"}


def active_reference_session(at: datetime | None = None) -> str:
    now = at or datetime.now(timezone.utc)
    korea = now.astimezone(ZoneInfo("Asia/Seoul"))
    korea_minutes = korea.hour * 60 + korea.minute
    if korea.weekday() < 5 and 9 * 60 <= korea_minutes < 15 * 60 + 30:
        return "korea"
    us = now.astimezone(ZoneInfo("America/New_York"))
    us_minutes = us.hour * 60 + us.minute
    if us.weekday() < 5 and 9 * 60 + 30 <= us_minutes < 16 * 60:
        return "us"
    return ""


def skipped_quote(symbol: str, reason: str) -> Dict[str, Any]:
    return {"ok": False, "symbol": symbol, "skipped": reason}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", "N/A"):
            return default
        text = str(value).strip().replace("$", "").replace("%", "").replace(",", "")
        return float(text)
    except (TypeError, ValueError):
        return default


def fetch_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url: str, timeout: int = 20) -> Dict[str, Any]:
    return json.loads(fetch_text(url, timeout=timeout))


def parse_nasdaq_quote(payload: Dict[str, Any], fetched_at_ms: int | None = None) -> Dict[str, Any]:
    data = payload.get("data") or {}
    primary = data.get("primaryData") or {}
    secondary = data.get("secondaryData") or {}
    last = safe_float(primary.get("lastSalePrice"))
    bid = safe_float(primary.get("bidPrice"))
    ask = safe_float(primary.get("askPrice"))
    if last <= 0:
        return {"ok": False, "error": "missing Nasdaq primary price", "fetched_at_ms": fetched_at_ms or int(time.time() * 1000)}
    return {
        "ok": True,
        "symbol": data.get("symbol") or "",
        "name": data.get("companyName") or "",
        "market_status": data.get("marketStatus") or "unknown",
        "last": last,
        "bid": bid,
        "ask": ask,
        "mid": (bid + ask) / 2.0 if bid > 0 and ask > 0 else last,
        "change": safe_float(primary.get("netChange")),
        "change_pct": safe_float(primary.get("percentageChange")),
        "quote_time": primary.get("lastTradeTimestamp") or "",
        "regular_close": safe_float(secondary.get("lastSalePrice"), last),
        "regular_change_pct": safe_float(secondary.get("percentageChange"), safe_float(primary.get("percentageChange"))),
        "regular_quote_time": secondary.get("lastTradeTimestamp") or "",
        "volume": safe_float(primary.get("volume")),
        "fetched_at_ms": fetched_at_ms or int(time.time() * 1000),
        "source": "nasdaq_public",
    }


def fetch_nasdaq_quote(symbol: str, asset_class: str) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    try:
        payload = fetch_json(NASDAQ_URL.format(symbol=urllib.parse.quote(symbol), asset_class=asset_class))
        return parse_nasdaq_quote(payload, now_ms)
    except Exception as exc:
        return {"ok": False, "symbol": symbol, "error": f"{type(exc).__name__}: {exc}", "fetched_at_ms": now_ms, "source": "nasdaq_public"}


def _naver_rate_blocks(page: str) -> List[tuple[bool, str, str]]:
    """Split real KRX/NXT quote blocks by their opening div positions.

    Naver nests many divs inside each block, so matching closing tags with a regex
    is unreliable. Splitting between the two known opening divs is stable and also
    avoids false hits from JavaScript strings that mention rate_info_*.
    """
    opening = re.compile(
        r'<div(?=[^>]*class="rate_info")(?=[^>]*id="rate_info_(krx|nxt)")([^>]*)>',
        re.I,
    )
    starts = list(opening.finditer(page))
    rows: List[tuple[bool, str, str]] = []
    for idx, match in enumerate(starts):
        end = starts[idx + 1].start() if idx + 1 < len(starts) else min(len(page), match.start() + 20_000)
        attrs = match.group(2)
        rows.append(("display: none" not in attrs.lower(), match.group(1).upper(), page[match.start():end]))
    return rows


def _parse_naver_block(block: str) -> Dict[str, float]:
    numbers = [safe_float(x) for x in re.findall(r'class="blind"[^>]*>\s*([0-9,.]+)\s*<', block, re.I)]
    if not numbers:
        return {"last": 0.0, "change": 0.0, "change_pct": 0.0}
    last = numbers[0]
    change = numbers[1] if len(numbers) > 1 else 0.0
    pct = numbers[2] if len(numbers) > 2 else (change / max(last - change, 1e-9) * 100.0)
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", block))
    if "하락" in text:
        change, pct = -abs(change), -abs(pct)
    elif "상승" in text:
        change, pct = abs(change), abs(pct)
    return {"last": last, "change": change, "change_pct": pct}


def _visible_rate_block(page: str) -> tuple[str, str]:
    rows = _naver_rate_blocks(page)
    if not rows:
        return "unknown", page
    parsed = [(visible, session, block, _parse_naver_block(block)) for visible, session, block in rows]
    active = [row for row in parsed if abs(row[3]["change_pct"]) > 0.001]
    if active:
        visible_active = [row for row in active if row[0]]
        chosen = (visible_active or active)[-1]
    else:
        visible_rows = [row for row in parsed if row[0]]
        chosen = (visible_rows or parsed)[-1]
    return chosen[1], chosen[2]


def parse_naver_quote(page: str, code: str, name: str, fetched_at_ms: int | None = None) -> Dict[str, Any]:
    session, block = _visible_rate_block(page)
    values = _parse_naver_block(block)
    last, change, pct = values["last"], values["change"], values["change_pct"]
    timestamp_match = re.search(r'(\d{4}\.\d{2}\.\d{2})\s+(\d{2}:\d{2})', page)
    quote_time = " ".join(timestamp_match.groups()) if timestamp_match else ""
    quote_ts_ms = 0
    if quote_time:
        try:
            kst = timezone(timedelta(hours=9))
            quote_ts_ms = int(datetime.strptime(quote_time, "%Y.%m.%d %H:%M").replace(tzinfo=kst).timestamp() * 1000)
        except ValueError:
            quote_ts_ms = 0
    if last <= 0:
        return {"ok": False, "symbol": code, "name": name, "error": "Naver price not found", "fetched_at_ms": fetched_at_ms or int(time.time() * 1000)}
    return {
        "ok": True,
        "symbol": code,
        "name": name,
        "session": session,
        "last": last,
        "change": change,
        "change_pct": pct,
        "quote_time": quote_time,
        "quote_ts_ms": quote_ts_ms,
        "fetched_at_ms": fetched_at_ms or int(time.time() * 1000),
        "source": "naver_finance_public",
    }


def fetch_naver_quote(code: str, name: str) -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    try:
        return parse_naver_quote(fetch_text(NAVER_URL.format(code=code)), code, name, now_ms)
    except Exception as exc:
        return {"ok": False, "symbol": code, "name": name, "error": f"{type(exc).__name__}: {exc}", "fetched_at_ms": now_ms, "source": "naver_finance_public"}


def okx_json(path: str, params: Dict[str, str]) -> Dict[str, Any]:
    return fetch_json(OKX_BASE + path + "?" + urllib.parse.urlencode(params))


def fetch_okx_snapshot() -> Dict[str, Any]:
    now_ms = int(time.time() * 1000)
    try:
        ticker = (okx_json("/api/v5/market/ticker", {"instId": "DRAM-USDT-SWAP"}).get("data") or [{}])[0]
        index = (okx_json("/api/v5/market/index-tickers", {"instId": "DRAM-USDT"}).get("data") or [{}])[0]
        mark = (okx_json("/api/v5/public/mark-price", {"instType": "SWAP", "instId": "DRAM-USDT-SWAP"}).get("data") or [{}])[0]
        funding = (okx_json("/api/v5/public/funding-rate", {"instId": "DRAM-USDT-SWAP"}).get("data") or [{}])[0]
        book = (okx_json("/api/v5/market/books", {"instId": "DRAM-USDT-SWAP", "sz": "5"}).get("data") or [{}])[0]
        bids, asks = book.get("bids") or [], book.get("asks") or []
        bid = safe_float(ticker.get("bidPx")); ask = safe_float(ticker.get("askPx")); last = safe_float(ticker.get("last"))
        bid_depth = sum(safe_float(x[0]) * safe_float(x[1]) for x in bids if len(x) >= 2)
        ask_depth = sum(safe_float(x[0]) * safe_float(x[1]) for x in asks if len(x) >= 2)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        return {
            "ok": last > 0,
            "symbol": "DRAM-USDT-SWAP",
            "last": last, "bid": bid, "ask": ask, "mid": mid,
            "index": safe_float(index.get("idxPx")), "mark": safe_float(mark.get("markPx")),
            "funding_rate": safe_float(funding.get("fundingRate")),
            "next_funding_time": int(safe_float(funding.get("nextFundingTime"))),
            "spread_pct": ((ask - bid) / mid * 100.0) if mid > 0 and ask >= bid else 0.0,
            "bid_depth_notional": bid_depth, "ask_depth_notional": ask_depth,
            "orderbook_imbalance": (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9),
            "open24h": safe_float(ticker.get("open24h")), "high24h": safe_float(ticker.get("high24h")), "low24h": safe_float(ticker.get("low24h")),
            "fetched_at_ms": now_ms, "source": "okx_public",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "fetched_at_ms": now_ms, "source": "okx_public"}


def _reference_price(etf: Dict[str, Any]) -> float:
    bid, ask, last = safe_float(etf.get("bid")), safe_float(etf.get("ask")), safe_float(etf.get("last"))
    if bid > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 999.0
        if spread_pct <= 0.5:
            return mid
    return last


def _reference_meta(etf: Dict[str, Any]) -> Dict[str, Any]:
    bid, ask = safe_float(etf.get("bid")), safe_float(etf.get("ask"))
    mid = (bid + ask) / 2.0 if bid > 0 and ask >= bid else 0.0
    spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 0.0
    source = "bid_ask_mid" if mid > 0 and spread_pct <= 0.5 else "last_trade"
    return {"source": source, "etf_spread_pct": spread_pct}


def classify_directional_forecast(*, session: str, reference_returns: dict[int, float], okx_returns: dict[int, float], reference_valid: bool) -> dict[str, object]:
    """Classify next short move separately from lead/lag convergence."""
    reasons = []
    result = {"bias": "neutral", "confidence": 0, "reasons": reasons, "advisory_only": True}
    if not reference_valid:
        reasons.append("reference_not_fresh")
        return result
    short = float(reference_returns.get(5, 0.0))
    medium = float(reference_returns.get(15, 0.0))
    if abs(short) < 0.5 or abs(medium) < 0.5 or short * medium <= 0:
        reasons.append("conflicting_reference_momentum")
        return result
    okx_short = float(okx_returns.get(5, 0.0))
    okx_medium = float(okx_returns.get(15, 0.0))
    direction = "up" if short > 0 else "down"
    already_led = ((okx_short - short > 0.5 and okx_medium - medium > 0.5) if direction == "up" else (okx_short - short < -0.5 and okx_medium - medium < -0.5))
    if already_led:
        reasons.append("okx_already_leads")
        return result
    result["bias"] = direction
    result["confidence"] = 70 if session == "korea" else 60
    reasons.append(f"reference_leads_{direction}")
    return result


def build_signal(*, now_ms: int, etf: Dict[str, Any], okx: Dict[str, Any], components: Iterable[Dict[str, Any]], max_age_ms: int = 180_000) -> Dict[str, Any]:
    components = list(components)
    reasons: List[str] = []
    etf_age = now_ms - int(etf.get("fetched_at_ms") or 0)
    okx_age = now_ms - int(okx.get("fetched_at_ms") or 0)
    fresh = bool(etf.get("ok") and okx.get("ok") and etf_age <= max_age_ms and okx_age <= max_age_ms)
    if not etf.get("ok"):
        reasons.append("missing_reference")
    elif etf_age > max_age_ms:
        reasons.append("stale_reference")
    if not okx.get("ok"):
        reasons.append("missing_okx")
    elif okx_age > max_age_ms:
        reasons.append("stale_okx")
    ref = _reference_price(etf)
    ref_meta = _reference_meta(etf)
    okx_mid = safe_float(okx.get("mid")) or ((safe_float(okx.get("bid")) + safe_float(okx.get("ask"))) / 2.0)
    deviation = ((okx_mid / ref) - 1.0) * 100.0 if fresh and ref > 0 and okx_mid > 0 else 0.0
    valid_changes = []
    for row in components:
        if not row.get("ok"):
            continue
        change = row.get("regular_change_pct") if row.get("symbol") == "MU" else row.get("change_pct")
        valid_changes.append(safe_float(change))
    positive = sum(1 for x in valid_changes if x > 0.05)
    negative = sum(1 for x in valid_changes if x < -0.05)
    flat = len(valid_changes) - positive - negative
    direction = "neutral"
    threshold = 0.25
    equity_open = str(etf.get("market_status", "")).lower() in {"market open", "open", "regular market"}
    if fresh and not equity_open:
        reasons.append("equity_market_closed")
    elif fresh and deviation >= threshold:
        direction = "okx_premium"
        reasons.append("okx_above_etf_reference")
    elif fresh and deviation <= -threshold:
        direction = "okx_discount"
        reasons.append("okx_below_etf_reference")
    elif fresh:
        reasons.append("deviation_inside_observation_band")
    confidence = 0
    if fresh:
        confidence = min(100, 45 + min(len(valid_changes), 3) * 10 + (15 if abs(deviation) >= threshold else 0))
    return {
        "data_fresh": fresh,
        "reference_price": ref,
        "reference_source": ref_meta["source"],
        "etf_spread_pct": ref_meta["etf_spread_pct"],
        "okx_mid": okx_mid,
        "deviation_pct": deviation,
        "direction": direction,
        "confidence": confidence,
        "reasons": reasons,
        "component_breadth": {"valid": len(valid_changes), "positive": positive, "negative": negative, "flat": flat, "average_change_pct": sum(valid_changes) / len(valid_changes) if valid_changes else 0.0},
        "advisory_only": True,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS snapshots (
      ts_ms INTEGER PRIMARY KEY,
      etf_price REAL, etf_change_pct REAL, etf_market_status TEXT,
      mu_change_pct REAL, samsung_change_pct REAL, hynix_change_pct REAL,
      okx_last REAL, okx_index REAL, okx_mark REAL, okx_bid REAL, okx_ask REAL,
      deviation_pct REAL, funding_rate REAL, spread_pct REAL,
      direction TEXT, confidence INTEGER, payload_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts_ms);
    """)


def save_snapshot(db_path: Path, report: Dict[str, Any]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    q = report["quotes"]; signal = report["signal"]
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        ensure_schema(conn)
        conn.execute("""INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            report["generated_at_ms"], q["dram_etf"].get("last"), q["dram_etf"].get("regular_change_pct"), q["dram_etf"].get("market_status"),
            q["micron"].get("regular_change_pct"), q["samsung"].get("change_pct"), q["sk_hynix"].get("change_pct"),
            q["okx"].get("last"), q["okx"].get("index"), q["okx"].get("mark"), q["okx"].get("bid"), q["okx"].get("ask"),
            signal.get("deviation_pct"), q["okx"].get("funding_rate"), q["okx"].get("spread_pct"), signal.get("direction"), signal.get("confidence"),
            json.dumps(report, ensure_ascii=False, sort_keys=True),
        ))
        conn.commit()


def write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def collect(max_age_ms: int = 180_000, at: datetime | None = None) -> Dict[str, Any]:
    now = at or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    session = active_reference_session(now)
    if not session:
        return {"generated_at_ms": now_ms, "skipped": True, "skip_reason": "outside_reference_sessions"}
    if session == "us":
        etf = fetch_nasdaq_quote("DRAM", "etf")
        micron = fetch_nasdaq_quote("MU", "stocks")
        if any(str(row.get("market_status", "")).lower() not in OPEN_STATUSES for row in (etf, micron)):
            return {"generated_at_ms": now_ms, "skipped": True, "skip_reason": "us_reference_market_closed"}
        samsung = skipped_quote("005930", "korea_session_closed")
        hynix = skipped_quote("000660", "korea_session_closed")
    else:
        samsung = fetch_naver_quote("005930", "Samsung Electronics")
        hynix = fetch_naver_quote("000660", "SK Hynix")
        korea_fresh = all(
            row.get("ok") and str(row.get("session", "")).upper() in {"KRX", "NXT"}
            and 0 <= now_ms - int(row.get("quote_ts_ms") or 0) <= max_age_ms
            for row in (samsung, hynix)
        )
        if not korea_fresh:
            return {"generated_at_ms": now_ms, "skipped": True, "skip_reason": "korea_reference_closed_or_stale"}
        etf = skipped_quote("DRAM", "us_session_closed")
        micron = skipped_quote("MU", "us_session_closed")
    okx = fetch_okx_snapshot()
    signal = build_signal(now_ms=now_ms, etf=etf, okx=okx, components=[micron, samsung, hynix], max_age_ms=max_age_ms)
    return {
        "generated_at_ms": now_ms,
        "active_session": session,
        "source": "public_read_only_cross_market",
        "note": "Observation only. No order, position, config, or trading action.",
        "quotes": {"dram_etf": etf, "micron": micron, "samsung": samsung, "sk_hynix": hynix, "okx": okx},
        "signal": signal,
    }


def compact_summary(report: Dict[str, Any]) -> str:
    q, s = report["quotes"], report["signal"]
    return (
        f"DRAM ETF={q['dram_etf'].get('last', 0):.4f} status={q['dram_etf'].get('market_status', 'unknown')} "
        f"OKX={q['okx'].get('last', 0):.4f} index={q['okx'].get('index', 0):.4f} "
        f"deviation={s.get('deviation_pct', 0):+.4f}% signal={s.get('direction')} confidence={s.get('confidence')} "
        f"MU={q['micron'].get('regular_change_pct', 0):+.2f}% Samsung={q['samsung'].get('change_pct', 0):+.2f}% Hynix={q['sk_hynix'].get('change_pct', 0):+.2f}%"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DRAM ETF / OKX cross-market monitor")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--max-age-sec", type=int, default=180)
    parser.add_argument("--json", action="store_true", help="Print full JSON instead of compact summary")
    args = parser.parse_args()
    report = collect(max_age_ms=max(30, args.max_age_sec) * 1000)
    if report.get("skipped"):
        print(f"advisory_only | skipped={report['skip_reason']}")
        return 0
    save_snapshot(args.db, report)
    write_json_atomic(args.report, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True) if args.json else compact_summary(report))
    return 0 if report["quotes"]["okx"].get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
