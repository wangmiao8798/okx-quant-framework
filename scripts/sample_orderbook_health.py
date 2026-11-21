#!/usr/bin/env python3
"""Read-only OKX orderbook health sampler with quality score."""
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_SYMBOLS = ["CRV-USDT-SWAP", "DRAM-USDT-SWAP", "FARTCOIN-USDT-SWAP"]


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_books(symbol: str, depth: int) -> dict:
    url = "https://www.okx.com/api/v5/market/books?" + urllib.parse.urlencode({"instId": symbol, "sz": str(depth)})
    data = fetch_json(url)
    rows = data.get("data") or []
    if not rows:
        raise RuntimeError(data.get("msg") or "empty book")
    return rows[0]


def quality_from_book(spread_pct: float, bid_depth: float, ask_depth: float, imbalance: float) -> tuple[float, str]:
    min_depth = min(bid_depth, ask_depth)
    spread_penalty = min(45.0, spread_pct * 350.0)
    depth_penalty = 0.0 if min_depth >= 20000 else min(25.0, (20000 - min_depth) / 800.0)
    imbalance_penalty = min(30.0, abs(imbalance) * 45.0)
    score = max(0.0, 100.0 - spread_penalty - depth_penalty - imbalance_penalty)
    if score >= 75:
        suggestion = "healthy"
    elif score >= 55:
        suggestion = "normal"
    elif spread_pct > 0.08 or min_depth < 8000:
        suggestion = "widen_or_reduce_size"
    else:
        suggestion = "cautious"
    return round(score, 2), suggestion


def summarize_book(symbol: str, book: dict) -> dict:
    bids = [(float(row[0]), float(row[1])) for row in book.get("bids", []) if len(row) >= 2]
    asks = [(float(row[0]), float(row[1])) for row in book.get("asks", []) if len(row) >= 2]
    if not bids or not asks:
        return {"symbol": symbol, "ok": False, "error": "missing bid/ask"}
    bid_px, bid_sz = bids[0]
    ask_px, ask_sz = asks[0]
    mid = (bid_px + ask_px) / 2
    spread_pct = (ask_px - bid_px) / mid * 100 if mid > 0 else 0.0
    bid_depth = sum(px * sz for px, sz in bids)
    ask_depth = sum(px * sz for px, sz in asks)
    imbalance = (bid_depth - ask_depth) / max(bid_depth + ask_depth, 1e-9)
    quality_score, suggestion = quality_from_book(spread_pct, bid_depth, ask_depth, imbalance)
    return {
        "symbol": symbol,
        "ok": True,
        "bid": bid_px,
        "ask": ask_px,
        "mid": round(mid, 8),
        "spread_pct": round(spread_pct, 5),
        "top_bid_sz": bid_sz,
        "top_ask_sz": ask_sz,
        "bid_depth_notional": round(bid_depth, 2),
        "ask_depth_notional": round(ask_depth, 2),
        "min_depth_notional": round(min(bid_depth, ask_depth), 2),
        "imbalance": round(imbalance, 4),
        "quality_score": quality_score,
        "suggestion": suggestion,
        "ts": int(time.time()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only orderbook health sampler")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="comma-separated OKX symbols")
    parser.add_argument("--depth", type=int, default=5, help="book depth")
    parser.add_argument("--out", type=Path, default=Path("data/orderbook_health.json"), help="output JSON path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    payload = {"generated_at": int(time.time()), "depth": args.depth, "symbols": {}}
    for symbol in symbols:
        try:
            payload["symbols"][symbol] = summarize_book(symbol, fetch_books(symbol, args.depth))
        except Exception as exc:
            payload["symbols"][symbol] = {"symbol": symbol, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for symbol, row in payload["symbols"].items():
        if not row.get("ok"):
            print(symbol, "ERROR", row.get("error"))
            continue
        print("{} mid={} spread={}% depth={}/{} imbalance={} score={} suggestion={}".format(
            symbol, row["mid"], row["spread_pct"], row["bid_depth_notional"], row["ask_depth_notional"],
            row["imbalance"], row["quality_score"], row["suggestion"]
        ))


if __name__ == "__main__":
    main()
