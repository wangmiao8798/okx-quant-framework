#!/usr/bin/env python3
"""Read-only full OKX USDT-swap market scanner.

The scanner produces research candidates only. It never calls private or
order APIs. Signals are deliberately conservative: all three timeframes must
agree, the latest incomplete candle is excluded, and adverse carry or trading
costs invalidate a candidate.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


UA = {"User-Agent": "Mozilla/5.0 full-market-scanner/2.0"}
MIN_NOTIONAL_M = 20.0
MAX_SELECTED = 35
MAX_API_RETRIES = 3
TAKER_FEE_RATE = 0.0005
MAX_ADVERSE_FUNDING = 0.0005
MAX_SPREAD_PCT = 0.03
MIN_TREND_EDGE_PCT = 0.15
SIGNAL_VALIDITY_BARS = 2
DEFAULT_DB_PATH = os.environ.get(
    "FULL_MARKET_SCANNER_DB", "/opt/okx_quant/data/full_market_scanner.db"
)
HORIZONS_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}
BAR_SECONDS = {"15": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}


def api(path):
    """Call a public endpoint with bounded retry/backoff."""
    last_error = None
    for attempt in range(MAX_API_RETRIES):
        try:
            req = urllib.request.Request("https://www.okx.com" + path, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.load(response)
            if str(payload.get("code", "0")) != "0":
                raise RuntimeError(payload.get("msg", "okx error"))
            return payload.get("data", [])
        except (OSError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < MAX_API_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    raise last_error or RuntimeError("public API failed")


def ema(xs, n):
    if not xs:
        return 0.0
    alpha = 2 / (n + 1)
    value = xs[0]
    for item in xs[1:]:
        value = alpha * item + (1 - alpha) * value
    return value


def rsi(xs, n=14):
    deltas = [xs[i] - xs[i - 1] for i in range(1, len(xs))][-n:]
    if not deltas:
        return 50.0
    gains = sum(max(item, 0) for item in deltas) / len(deltas)
    losses = sum(max(-item, 0) for item in deltas) / len(deltas)
    if gains == 0 and losses == 0:
        return 50.0
    return 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)


def _closed_candles(symbol, bar, limit=90):
    rows = list(reversed(api(f"/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}")))
    if len(rows) < 65:
        raise RuntimeError(f"insufficient_{bar}_candles")
    # OKX returns the current, still-forming candle. Excluding it avoids
    # signals that flip solely because the current candle is incomplete.
    return rows[:-1]


def features(symbol):
    ticker = api("/api/v5/market/ticker?instId=" + symbol)[0]
    last = float(ticker["last"])
    opened = float(ticker["open24h"])
    volume = float(ticker.get("volCcy24h") or 0)
    book = api("/api/v5/market/books?instId=" + symbol + "&sz=20")[0]
    bid = float(book["bids"][0][0])
    ask = float(book["asks"][0][0])
    bid_notional = sum(float(row[0]) * float(row[1]) for row in book["bids"])
    ask_notional = sum(float(row[0]) * float(row[1]) for row in book["asks"])
    book_total = bid_notional + ask_notional
    imbalance = (bid_notional - ask_notional) / book_total if book_total else 0.0
    funding = float(api("/api/v5/public/funding-rate?instId=" + symbol)[0]["fundingRate"])
    output = {
        "symbol": symbol,
        "last": last,
        "notional_m": last * volume / 1e6,
        "change24": (last / opened - 1) * 100 if opened > 0 else 0.0,
        "funding": funding,
        "spread_pct": (ask / bid - 1) * 100 if bid > 0 else 99.0,
        "book_imbalance": imbalance,
    }
    for bar, key in (("15m", "15"), ("1H", "1h"), ("4H", "4h")):
        candles = _closed_candles(symbol, bar)
        closes = [float(row[4]) for row in candles]
        volumes = [float(row[5]) for row in candles]
        if not closes or closes[-1] <= 0:
            raise RuntimeError(f"invalid_{bar}_candles")
        output["rsi" + key] = rsi(closes)
        output["ema20_" + key] = (closes[-1] / ema(closes[-60:], 20) - 1) * 100
        output["ema50_" + key] = (closes[-1] / ema(closes, 50) - 1) * 100
        output["candle_ts_" + key] = int(candles[-1][0])
        output["candle_age_sec_" + key] = max(
            0.0, time.time() - output["candle_ts_" + key] / 1000
        )
        if key == "15":
            baseline = sum(volumes[-25:-5]) / max(1, len(volumes[-25:-5]))
            output["vol_ratio15"] = (
                sum(volumes[-5:]) / max(1, len(volumes[-5:])) / baseline
                if baseline > 0
                else 0.0
            )
    output["round_trip_cost_pct"] = (
        output["spread_pct"] + 2 * TAKER_FEE_RATE * 100 + abs(funding) * 100
    )
    output["trend_strength_pct"] = (
        abs(output["ema20_1h"]) + abs(output["ema20_4h"])
    )
    output["signal_key_base"] = f"{symbol}:{output['candle_ts_15']}"
    return output


def score_candidate(x, direction):
    reasons = []
    score = 0
    if direction not in {"long", "short"}:
        return {"symbol": x.get("symbol"), "direction": direction, "score": 0, "eligible": False, "reasons": ["invalid_direction"]}
    if x.get("notional_m", 0) < MIN_NOTIONAL_M:
        reasons.append("thin_liquidity")
    if x.get("spread_pct", 99) > MAX_SPREAD_PCT:
        reasons.append("wide_spread")
    if any(x.get("candle_age_sec_" + key, 10**9) > BAR_SECONDS[key] * 2 for key in BAR_SECONDS):
        reasons.append("stale_candles")

    sign = 1 if direction == "long" else -1
    trend20 = [x.get("ema20_15", 0), x.get("ema20_1h", 0), x.get("ema20_4h", 0)]
    trend50 = [x.get("ema50_15", 0), x.get("ema50_1h", 0), x.get("ema50_4h", 0)]
    # Require the entry timeframe to agree with the higher timeframes. This
    # removes the previous false positives where 1H/4H were bullish while
    # 15m was still falling.
    if all(sign * value > 0 for value in trend20):
        score += 3
    else:
        reasons.append("timeframe_misalignment")
    if all(sign * value > 0 for value in trend50):
        score += 2
    else:
        reasons.append("slow_trend_misalignment")

    if direction == "long":
        if 42 <= x.get("rsi15", 0) <= 70:
            score += 1
        if x.get("rsi1h", 0) >= 80 or x.get("rsi4h", 0) >= 80 or x.get("change24", 0) > 12:
            reasons.append("overheated")
        if x.get("book_imbalance", 0) > 0.05:
            score += 1
        if x.get("funding", 0) > MAX_ADVERSE_FUNDING:
            reasons.append("adverse_funding")
    else:
        if 30 <= x.get("rsi15", 0) <= 58:
            score += 1
        if x.get("rsi1h", 100) < 18 or x.get("rsi4h", 100) < 25 or x.get("change24", 0) < -15:
            reasons.append("oversold")
        if x.get("book_imbalance", 0) < -0.05:
            score += 1
        if x.get("funding", 0) < -MAX_ADVERSE_FUNDING:
            reasons.append("adverse_funding")

    if x.get("vol_ratio15", 0) >= 1.2:
        score += 1
    if x.get("trend_strength_pct", 0) < max(MIN_TREND_EDGE_PCT, x.get("round_trip_cost_pct", 0) * 1.5):
        reasons.append("edge_below_cost")

    eligible = score >= 7 and not reasons
    return {
        "symbol": x.get("symbol"),
        "direction": direction,
        "score": score,
        "eligible": eligible,
        "reasons": reasons,
        "signal_key": f"{x.get('signal_key_base', x.get('symbol'))}:{direction}",
        # The selected candle is already closed. Keep the candidate valid
        # through the next candle as long as the freshness gate still holds.
        "valid_until_ts": int(x.get("candle_ts_15", 0)) + SIGNAL_VALIDITY_BARS * 15 * 60 * 1000,
        "round_trip_cost_pct": round(x.get("round_trip_cost_pct", 0), 5),
        "trend_strength_pct": round(x.get("trend_strength_pct", 0), 5),
    }


def _connect_db(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scanner_signals (
            signal_key TEXT PRIMARY KEY,
            captured_at_ms INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            score INTEGER NOT NULL,
            eligible INTEGER NOT NULL,
            selected INTEGER NOT NULL,
            round_trip_cost_pct REAL NOT NULL,
            trend_strength_pct REAL NOT NULL,
            reasons_json TEXT NOT NULL,
            features_json TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scanner_outcomes (
            signal_key TEXT NOT NULL,
            horizon TEXT NOT NULL,
            observed_at_ms INTEGER NOT NULL,
            observed_delay_sec REAL NOT NULL,
            exit_price REAL NOT NULL,
            gross_return_pct REAL NOT NULL,
            net_return_pct REAL NOT NULL,
            data_quality TEXT NOT NULL,
            PRIMARY KEY (signal_key, horizon),
            FOREIGN KEY (signal_key) REFERENCES scanner_signals(signal_key)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scanner_runs (
            captured_at_ms INTEGER PRIMARY KEY,
            universe_count INTEGER NOT NULL,
            scanned_count INTEGER NOT NULL,
            failure_count INTEGER NOT NULL,
            selected_count INTEGER NOT NULL
        )"""
    )
    conn.commit()
    return conn


def persist_scan(db_path, captured_at_ms, universe_count, scanned_count, failures, rows, selected_keys):
    """Persist every evaluated direction; only selected eligible signals get labels."""
    conn = _connect_db(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO scanner_runs VALUES (?, ?, ?, ?, ?)",
            (captured_at_ms, universe_count, scanned_count, len(failures), len(selected_keys)),
        )
        for item in rows:
            features_json = json.dumps(item, ensure_ascii=False, sort_keys=True)
            for direction in ("long", "short"):
                decision = item[direction]
                signal_key = decision["signal_key"]
                conn.execute(
                    """INSERT OR IGNORE INTO scanner_signals
                    (signal_key, captured_at_ms, symbol, direction, entry_price,
                     score, eligible, selected, round_trip_cost_pct,
                     trend_strength_pct, reasons_json, features_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        signal_key,
                        captured_at_ms,
                        item["symbol"],
                        direction,
                        item["last"],
                        decision["score"],
                        int(decision["eligible"]),
                        int(signal_key in selected_keys),
                        decision["round_trip_cost_pct"],
                        decision["trend_strength_pct"],
                        json.dumps(decision["reasons"], ensure_ascii=False),
                        features_json,
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _public_ticker_map(symbols):
    rows = api("/api/v5/market/tickers?instType=SWAP")
    wanted = set(symbols)
    result = {}
    for row in rows:
        symbol = row.get("instId")
        if symbol in wanted:
            try:
                result[symbol] = float(row["last"])
            except (KeyError, TypeError, ValueError):
                continue
    return result


def resolve_forward_outcomes(db_path):
    """Label due selected signals with the first available public ticker.

    The observed delay is stored explicitly. This is a forward-monitoring
    label, not a substitute for candle-exact backtesting.
    """
    now_ms = int(time.time() * 1000)
    conn = _connect_db(db_path)
    try:
        due = []
        for signal_key, captured_at, symbol, direction, entry, cost in conn.execute(
            """SELECT s.signal_key, s.captured_at_ms, s.symbol, s.direction,
                      s.entry_price, s.round_trip_cost_pct
               FROM scanner_signals s
               WHERE s.eligible=1 AND s.selected=1
                 AND NOT EXISTS (
                     SELECT 1 FROM scanner_outcomes o
                     WHERE o.signal_key=s.signal_key AND o.horizon=?
                 )
                 AND s.captured_at_ms + ? <= ?""",
            ("15m", HORIZONS_MS["15m"], now_ms),
        ):
            due.append((signal_key, captured_at, symbol, direction, entry, cost, "15m"))
        # Add the longer horizons separately so one run can resolve each due label.
        for horizon, horizon_ms in HORIZONS_MS.items():
            if horizon == "15m":
                continue
            for signal_key, captured_at, symbol, direction, entry, cost in conn.execute(
                """SELECT s.signal_key, s.captured_at_ms, s.symbol, s.direction,
                          s.entry_price, s.round_trip_cost_pct
                   FROM scanner_signals s
                   WHERE s.eligible=1 AND s.selected=1
                     AND NOT EXISTS (
                         SELECT 1 FROM scanner_outcomes o
                         WHERE o.signal_key=s.signal_key AND o.horizon=?
                     )
                     AND s.captured_at_ms + ? <= ?""",
                (horizon, horizon_ms, now_ms),
            ):
                due.append((signal_key, captured_at, symbol, direction, entry, cost, horizon))
        if not due:
            return 0
        prices = _public_ticker_map({item[2] for item in due})
        updated = 0
        for signal_key, captured_at, symbol, direction, entry, cost, horizon in due:
            exit_price = prices.get(symbol)
            if not exit_price or entry <= 0:
                continue
            sign = 1 if direction == "long" else -1
            gross = sign * (exit_price / entry - 1) * 100
            net = gross - cost
            observed_at = int(time.time() * 1000)
            conn.execute(
                """INSERT OR IGNORE INTO scanner_outcomes
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_key,
                    horizon,
                    observed_at,
                    max(0.0, (observed_at - captured_at - HORIZONS_MS[horizon]) / 1000),
                    exit_price,
                    gross,
                    net,
                    "ticker_at_or_after_horizon",
                ),
            )
            updated += 1
        conn.commit()
        return updated
    finally:
        conn.close()


def build_report(db_path):
    conn = _connect_db(db_path)
    try:
        report = {"db_path": str(db_path), "horizons": {}}
        for horizon in HORIZONS_MS:
            rows = list(
                conn.execute(
                    """SELECT net_return_pct FROM scanner_outcomes
                       WHERE horizon=?""",
                    (horizon,),
                )
            )
            values = [float(row[0]) for row in rows]
            wins = [value for value in values if value > 0]
            losses = [value for value in values if value < 0]
            gross_profit = sum(wins)
            gross_loss = abs(sum(losses))
            report["horizons"][horizon] = {
                "samples": len(values),
                "win_rate": len(wins) / len(values) if values else 0.0,
                "avg_net_return_pct": sum(values) / len(values) if values else 0.0,
                "profit_factor": gross_profit / gross_loss if gross_loss else (None if not wins else "inf"),
            }
        report["eligible_signals"] = conn.execute(
            "SELECT COUNT(*) FROM scanner_signals WHERE eligible=1 AND selected=1"
        ).fetchone()[0]
        return report
    finally:
        conn.close()


def scan(db_path=None):
    ticks = api("/api/v5/market/tickers?instType=SWAP")
    base = []
    for ticker in ticks:
        symbol = ticker.get("instId", "")
        if not symbol.endswith("-USDT-SWAP") or ticker.get("state", "live") not in ("live", ""):
            continue
        try:
            last = float(ticker["last"])
            opened = float(ticker["open24h"])
            notional = last * float(ticker.get("volCcy24h") or 0) / 1e6
            if last > 0 and opened > 0 and notional >= MIN_NOTIONAL_M:
                base.append((notional, symbol))
        except (KeyError, TypeError, ValueError):
            continue
    selected = sorted(base, reverse=True)[:MAX_SELECTED]
    rows = []
    failures = []
    for _notional, symbol in selected:
        try:
            item = features(symbol)
            item["long"] = score_candidate(item, "long")
            item["short"] = score_candidate(item, "short")
            rows.append(item)
        except Exception as exc:
            failures.append(
                {
                    "symbol": symbol,
                    "error": type(exc).__name__,
                    "message": str(exc)[:160],
                }
            )
    longs = sorted(
        [item for item in rows if item["long"]["eligible"]],
        key=lambda item: item["long"]["score"],
        reverse=True,
    )[:5]
    shorts = sorted(
        [item for item in rows if item["short"]["eligible"]],
        key=lambda item: item["short"]["score"],
        reverse=True,
    )[:5]
    captured_at = int(time.time() * 1000)
    if db_path:
        selected_keys = {
            item[direction]["signal_key"]
            for direction, items in (("long", longs), ("short", shorts))
            for item in items
        }
        persist_scan(db_path, captured_at, len(base), len(rows), failures, rows, selected_keys)
    return {
        "captured_at": captured_at,
        "mode": "read_only_shadow",
        "universe_count": len(base),
        "scanned_count": len(rows),
        "failures": failures,
        "longs": longs,
        "shorts": shorts,
        "note": "只读扫描，未调用私有接口，未自动下单；信号需去重、回测并通过风险门",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only OKX market scanner")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    try:
        if args.report:
            print(json.dumps(build_report(args.db_path), ensure_ascii=False))
        else:
            updated = resolve_forward_outcomes(args.db_path)
            result = scan(args.db_path)
            result["outcomes_updated"] = updated
            result["db_path"] = args.db_path
            print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc), "mode": "read_only_shadow"},
                ensure_ascii=False,
            )
        )
        sys.exit(1)
