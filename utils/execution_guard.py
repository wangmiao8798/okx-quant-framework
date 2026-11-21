"""Execution safety guards for live limit orders.

These helpers are intentionally conservative: they only block new non-reduce
limit orders that would cross the current best bid/ask or trade during obviously
poor top-of-book conditions. Market orders and reduce-only exits are not blocked.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Tuple

from utils.helpers import safe_float


class ExecutionGuard:
    def __init__(self, client: Any = None, cache_ttl: float = 2.0, book_file: str = "data/orderbook_health.json"):
        self.client = client
        self.cache_ttl = max(0.2, float(cache_ttl))
        self.book_file = Path(book_file)
        self._ticker_cache: Dict[str, Tuple[float, Dict[str, float]]] = {}
        self._book_cache: Tuple[float, Dict[str, Any]] = (0.0, {})

    def _ticker(self, symbol: str) -> Dict[str, float]:
        now = time.time()
        cached = self._ticker_cache.get(symbol)
        if cached and now - cached[0] <= self.cache_ttl:
            return cached[1]
        if not self.client or not symbol:
            return {}
        try:
            row = self.client.get_ticker(symbol) or {}
        except Exception:
            row = {}
        data = {
            "bid": safe_float(row.get("bid", 0)),
            "ask": safe_float(row.get("ask", 0)),
            "last": safe_float(row.get("last", 0)),
        }
        self._ticker_cache[symbol] = (now, data)
        return data


    def _book_quality(self, symbol: str) -> Dict[str, Any]:
        now = time.time()
        cached_ts, cached_data = self._book_cache
        if now - cached_ts > 5.0:
            try:
                cached_data = json.loads(self.book_file.read_text(encoding="utf-8"))
            except Exception:
                cached_data = {}
            self._book_cache = (now, cached_data)
        generated_at = safe_float(cached_data.get("generated_at", 0)) if isinstance(cached_data, dict) else 0.0
        if generated_at <= 0 or now - generated_at > 180:
            return {}
        symbols = cached_data.get("symbols", {}) if isinstance(cached_data, dict) else {}
        row = symbols.get(symbol, {}) if isinstance(symbols, dict) else {}
        return row if isinstance(row, dict) else {}

    def check_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        price: float,
        reduce_only: bool = False,
    ) -> Tuple[bool, str]:
        side = str(side or "").lower()
        order_type = str(order_type or "market").lower()
        if reduce_only or order_type != "limit":
            return True, "not_guarded"
        price = safe_float(price)
        if side not in {"buy", "sell"} or price <= 0:
            return True, "not_guarded"
        ticker = self._ticker(symbol)
        bid = safe_float(ticker.get("bid", 0))
        ask = safe_float(ticker.get("ask", 0))
        if bid <= 0 or ask <= 0 or ask <= bid:
            return True, "no_book"
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0 if mid > 0 else 0.0
        quality = self._book_quality(symbol)
        if quality.get("ok"):
            score = safe_float(quality.get("quality_score", 0))
            suggestion = str(quality.get("suggestion", ""))
            if score > 0 and score < 45:
                return False, f"poor_book_quality_{score:.2f}_{suggestion}"
        if spread_pct > 0.18:
            return False, f"wide_spread_{spread_pct:.4f}%"
        if side == "buy" and price >= ask:
            return False, f"maker_cross_buy px={price} ask={ask}"
        if side == "sell" and price <= bid:
            return False, f"maker_cross_sell px={price} bid={bid}"
        return True, "ok"
