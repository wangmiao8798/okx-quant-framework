#!/usr/bin/env python3
"""Build aligned-window returns and advisory forward outcomes from radar snapshots."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/cross_market_opportunity.db"
DEFAULT_CONFIG = ROOT / "config/cross_market_assets.yaml"
DEFAULT_REPORT = ROOT / "data/cross_market_forward_report.json"
DEFAULT_HORIZONS = [5, 15, 30, 60]


def _ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def source_is_fresh(db: Path, max_age_seconds: int, now: float | None = None) -> bool:
    if not db.exists():
        return False
    with sqlite3.connect(db, timeout=30) as conn:
        row = conn.execute("SELECT max(batch_id) FROM normalized_quotes").fetchone()
    latest = row[0] if row else None
    age = (now if now is not None else time.time()) - _ts(latest) if latest else float("inf")
    return 0 <= age <= max_age_seconds


def latest_benchmarks_open(db: Path, benchmarks: set[str]) -> bool:
    if not db.exists() or not benchmarks:
        return False
    with sqlite3.connect(db, timeout=30) as conn:
        row = conn.execute("SELECT max(batch_id) FROM normalized_quotes").fetchone()
        latest = row[0] if row else None
        payloads = conn.execute(
            f"SELECT symbol,payload_json FROM normalized_quotes WHERE batch_id=? AND symbol IN ({','.join('?' for _ in benchmarks)})",
            (latest, *sorted(benchmarks)),
        ).fetchall() if latest else []
    statuses = {symbol: str(json.loads(payload).get("market_status", "")).lower() for symbol, payload in payloads}
    return len(statuses) == len(benchmarks) and all(statuses.get(symbol) in {"market open", "open", "regular market"} for symbol in benchmarks)


def _pct(start: float, end: float) -> float:
    return (end / start - 1.0) * 100.0 if start > 0 and end > 0 else 0.0


def _load_batches(conn: sqlite3.Connection) -> list[tuple[str, dict[str, dict[str, Any]]]]:
    batches: dict[str, dict[str, dict[str, Any]]] = {}
    for batch, symbol, price, age_seconds, ok, payload in conn.execute("SELECT batch_id,symbol,price,age_seconds,ok,payload_json FROM normalized_quotes ORDER BY batch_id,symbol"):
        row = json.loads(payload)
        row.update({"symbol": symbol, "price": float(price or 0), "age_seconds": float(age_seconds or 0), "ok": bool(ok)})
        batches.setdefault(batch, {})[symbol] = row
    return sorted(batches.items(), key=lambda item: _ts(item[0]))


def _prior(batches: list[tuple[str, dict[str, Any]]], end_index: int, minutes: int, tolerance: int):
    target = _ts(batches[end_index][0]) - minutes * 60
    candidates = [(abs(_ts(batch) - target), batch, rows) for batch, rows in batches[:end_index]]
    if not candidates:
        return None
    distance, batch, rows = min(candidates, key=lambda item: item[0])
    return (batch, rows, distance) if distance <= tolerance else None


def _theme_row(name: str, theme: dict[str, Any], start_batch: str | None, start: dict[str, Any] | None, end_batch: str, end: dict[str, Any], minutes: int, cfg: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    minimum = int(cfg.get("quality", {}).get("min_equities", 2))
    threshold = float(cfg.get("thresholds", {}).get("lead_pct", 2.0))
    crypto, benchmark = theme["crypto"], theme["benchmark"]
    if start is None:
        reasons.append("insufficient_history")
    start_benchmark = (start or {}).get(benchmark, {})
    current_benchmark = end.get(benchmark, {})
    eligible_statuses = {"market open", "open", "regular market"}
    if (start is not None and str(start_benchmark.get("market_status", "")).lower() not in eligible_statuses) or str(current_benchmark.get("market_status", "")).lower() not in eligible_statuses:
        reasons.append("equity_market_closed")
    valid = [s for s in theme["equities"] if start and start.get(s, {}).get("ok") and end.get(s, {}).get("ok")]
    if len(valid) < minimum:
        reasons.append("insufficient_equities")
    mandatory_rows = [start.get(crypto, {}), end.get(crypto, {}), start.get(benchmark, {}), end.get(benchmark, {})] if start else []
    mandatory_ok = bool(start and all(row.get("ok") for row in mandatory_rows))
    if not mandatory_ok:
        reasons.append("missing_mandatory_quote")
    all_window_rows = mandatory_rows + [start[s] for s in valid] + [end[s] for s in valid] if start else []
    max_age = int(cfg.get("quality", {}).get("max_age_seconds", 300))
    if any(float(row.get("age_seconds", 0)) > max_age for row in all_window_rows):
        reasons.append("stale_quote")

    crypto_return = benchmark_return = basket_return = excess = divergence = 0.0
    if not reasons:
        crypto_return = _pct(start[crypto]["price"], end[crypto]["price"])
        benchmark_return = _pct(start[benchmark]["price"], end[benchmark]["price"])
        basket_return = sum(_pct(start[s]["price"], end[s]["price"]) for s in valid) / len(valid)
        excess = basket_return - benchmark_return
        divergence = crypto_return - excess
    signal = "neutral"
    if not reasons:
        if divergence >= threshold:
            signal = "crypto_leads"
        elif divergence <= -threshold:
            signal = "equities_lead"
        else:
            reasons.append("inside_band")
    reasons.append("advisory_only")
    quality = "ok" if reasons in (["advisory_only"], ["inside_band", "advisory_only"]) else "bad"
    return {"end_batch_id": end_batch, "start_batch_id": start_batch, "theme": name, "window_minutes": minutes,
            "crypto_return_pct": crypto_return, "equity_basket_return_pct": basket_return,
            "benchmark_return_pct": benchmark_return, "equity_excess_pct": excess,
            "divergence_pct": divergence, "signal": signal, "data_quality": quality,
            "reasons": reasons, "advisory_only": True}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS aligned_window_returns(
      end_batch_id TEXT,theme TEXT,window_minutes INTEGER,start_batch_id TEXT,
      crypto_return_pct REAL,equity_basket_return_pct REAL,benchmark_return_pct REAL,
      equity_excess_pct REAL,divergence_pct REAL,signal TEXT,data_quality TEXT,
      reasons_json TEXT,payload_json TEXT,PRIMARY KEY(end_batch_id,theme,window_minutes));
    CREATE TABLE IF NOT EXISTS forward_outcomes(
      signal_batch_id TEXT,theme TEXT,signal_window_minutes INTEGER,forward_minutes INTEGER,
      outcome_batch_id TEXT,initial_divergence_pct REAL,outcome_divergence_pct REAL,
      gap_reduction_pct REAL,mfe_pct REAL,mae_pct REAL,estimated_cost_pct REAL,
      net_after_cost_pct REAL,favorable INTEGER,payload_json TEXT,
      PRIMARY KEY(signal_batch_id,theme,signal_window_minutes,forward_minutes));
    """)


def _write_aligned(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute("""INSERT OR REPLACE INTO aligned_window_returns
      (end_batch_id,theme,window_minutes,start_batch_id,crypto_return_pct,equity_basket_return_pct,
       benchmark_return_pct,equity_excess_pct,divergence_pct,signal,data_quality,reasons_json,payload_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (row["end_batch_id"], row["theme"], row["window_minutes"], row["start_batch_id"], row["crypto_return_pct"],
       row["equity_basket_return_pct"], row["benchmark_return_pct"], row["equity_excess_pct"], row["divergence_pct"],
       row["signal"], row["data_quality"], json.dumps(row["reasons"]), json.dumps(row, sort_keys=True)))


def _forward_rows(conn: sqlite3.Connection, batches, cfg, forward_horizons: list[int]) -> int:
    index = {batch: i for i, (batch, _) in enumerate(batches)}
    cost = float(cfg.get("thresholds", {}).get("estimated_round_trip_cost_pct", 0.0))
    written = 0
    signals = conn.execute("SELECT end_batch_id,theme,window_minutes,divergence_pct,signal FROM aligned_window_returns WHERE data_quality='ok' AND signal!='neutral'").fetchall()
    for signal_batch, theme_name, window, initial, signal in signals:
        if signal_batch not in index or theme_name not in cfg["themes"]:
            continue
        start_i = index[signal_batch]
        theme = cfg["themes"][theme_name]
        for forward in forward_horizons:
            target = _ts(signal_batch) + forward * 60
            candidates = [(abs(_ts(batch) - target), i) for i, (batch, _) in enumerate(batches) if i > start_i]
            if not candidates:
                continue
            distance, outcome_i = min(candidates)
            tolerance = int(cfg.get("quality", {}).get("window_tolerance_seconds", 90))
            if distance > tolerance:
                continue
            points = []
            signal_prices = batches[start_i][1]
            for i in range(start_i + 1, outcome_i + 1):
                _, future_prices = batches[i]
                check = _theme_row(theme_name, theme, signal_batch, signal_prices, batches[i][0], future_prices, forward, cfg)
                if check["data_quality"] != "ok":
                    points = []
                    break
                crypto_forward = _pct(signal_prices[theme["crypto"]]["price"], future_prices[theme["crypto"]]["price"])
                valid = [s for s in theme["equities"] if signal_prices.get(s, {}).get("ok") and future_prices.get(s, {}).get("ok")]
                equity_forward = sum(_pct(signal_prices[s]["price"], future_prices[s]["price"]) for s in valid) / len(valid)
                benchmark_forward = _pct(signal_prices[theme["benchmark"]]["price"], future_prices[theme["benchmark"]]["price"])
                equity_excess_forward = equity_forward - benchmark_forward
                # Positive means the side that lagged at signal time caught up afterward.
                convergence = equity_excess_forward - crypto_forward if signal == "crypto_leads" else crypto_forward - equity_excess_forward
                points.append(convergence)
            if not points:
                continue
            gap = points[-1]
            payload = {"signal_batch_id": signal_batch, "outcome_batch_id": batches[outcome_i][0], "theme": theme_name,
                       "signal": signal, "signal_window_minutes": window, "forward_minutes": forward,
                       "initial_divergence_pct": initial, "outcome_divergence_pct": initial-gap if signal == "crypto_leads" else initial+gap,
                       "gap_reduction_pct": gap, "mfe_pct": max([0.0, *points]), "mae_pct": min([0.0, *points]),
                       "estimated_cost_pct": cost, "net_after_cost_pct": gap-cost, "favorable": gap-cost > 0,
                       "advisory_only": True}
            conn.execute("""INSERT OR REPLACE INTO forward_outcomes
              (signal_batch_id,theme,signal_window_minutes,forward_minutes,outcome_batch_id,initial_divergence_pct,
               outcome_divergence_pct,gap_reduction_pct,mfe_pct,mae_pct,estimated_cost_pct,net_after_cost_pct,favorable,payload_json)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (signal_batch, theme_name, window, forward, payload["outcome_batch_id"], initial, payload["outcome_divergence_pct"],
               gap, payload["mfe_pct"], payload["mae_pct"], cost, payload["net_after_cost_pct"], int(payload["favorable"]), json.dumps(payload, sort_keys=True)))
            written += 1
    return written


def analyze_database(db: Path, cfg: dict[str, Any], horizons: list[int] | None = None, forward_horizons: list[int] | None = None) -> dict[str, Any]:
    horizons = horizons or DEFAULT_HORIZONS
    forward_horizons = forward_horizons or DEFAULT_HORIZONS
    with sqlite3.connect(db, timeout=30) as conn:
        _ensure_schema(conn)
        # Derived research tables are reproducible from normalized_quotes.
        # Rebuild them atomically so corrected/invalidated source rows cannot leave stale outcomes.
        conn.execute("DELETE FROM forward_outcomes")
        conn.execute("DELETE FROM aligned_window_returns")
        batches = _load_batches(conn)
        written = 0
        if batches:
            tolerance = int(cfg.get("quality", {}).get("window_tolerance_seconds", 90))
            latest = len(batches)-1
            for end_i, (end_batch, end) in enumerate(batches):
                for minutes in horizons:
                    prior = _prior(batches, end_i, minutes, tolerance)
                    if prior or end_i == latest:
                        start_batch, start = (prior[0], prior[1]) if prior else (None, None)
                        for name, theme in cfg["themes"].items():
                            _write_aligned(conn, _theme_row(name, theme, start_batch, start, end_batch, end, minutes, cfg)); written += 1
            forward_written = _forward_rows(conn, batches, cfg, forward_horizons)
        else:
            forward_written = 0
        conn.commit()
        directional = conn.execute("SELECT count(*) FROM aligned_window_returns WHERE data_quality='ok' AND signal!='neutral'").fetchone()[0]
        pending = conn.execute("SELECT count(*) FROM aligned_window_returns WHERE data_quality='bad'").fetchone()[0]
    return {"horizons": horizons, "forward_horizons": forward_horizons, "aligned_rows_written": written,
            "forward_rows_written": forward_written, "directional_signals": directional,
            "pending_or_bad_windows": pending, "advisory_only": True}


def main() -> int:
    p = argparse.ArgumentParser(description="Aligned-window and forward-outcome cross-market research")
    p.add_argument("--db", type=Path, default=DEFAULT_DB); p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    p.add_argument("--max-source-age-sec", type=int, default=420)
    p.add_argument("--force", action="store_true")
    args = p.parse_args(); cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    benchmarks = {theme["benchmark"] for theme in cfg.get("themes", {}).values()}
    if not args.force and (
        not source_is_fresh(args.db, max(1, args.max_source_age_sec))
        or not latest_benchmarks_open(args.db, benchmarks)
    ):
        print(json.dumps({"advisory_only": True, "skipped": "no_fresh_source_snapshot"}, sort_keys=True))
        return 0
    result = analyze_database(args.db, cfg)
    args.report.write_text(json.dumps(result, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
