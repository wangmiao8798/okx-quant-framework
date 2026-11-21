from __future__ import annotations
import json, time, urllib.parse, urllib.request
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class PublicMarketSnapshot:
    symbol: str
    now_ms: int
    market: Any

class OkxPublicMarket:
    """Independent public-data adapter. No private API or execution imports."""
    def __init__(self, base_url: str = "https://www.okx.com", timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}?{query}" if query else f"{self.base_url}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "okx-one-way-shadow/0.1"})
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            payload = json.load(response)
        if str(payload.get("code", "0")) != "0":
            raise RuntimeError(payload.get("msg", "okx public api error"))
        return payload.get("data", [])

    def universe(self, max_symbols: int = 30, min_notional_m: float = 20.0) -> list[str]:
        rows = self.get("/api/v5/market/tickers", {"instType": "SWAP"})
        candidates = []
        for row in rows:
            symbol = str(row.get("instId", ""))
            if not symbol.endswith("-USDT-SWAP") or row.get("state", "live") not in ("live", ""):
                continue
            try:
                last = float(row["last"])
                volume = float(row.get("volCcy24h") or 0.0)
                notional_m = last * volume / 1_000_000.0
                if last > 0 and notional_m >= min_notional_m:
                    candidates.append((notional_m, symbol))
            except (KeyError, TypeError, ValueError):
                continue
        return [symbol for _, symbol in sorted(candidates, reverse=True)[:max_symbols]]

    def snapshot(self, symbol: str, bars: tuple[str, ...] = ("15m", "1H", "4H"), limit: int = 90):
        from one_way.signal.scoring import NormalizedCandle, NormalizedFunding, NormalizedOrderBook, CandidateMarket
        now_ms = int(time.time() * 1000)
        ticker = self.get("/api/v5/market/ticker", {"instId": symbol})[0]
        book = self.get("/api/v5/market/books", {"instId": symbol, "sz": "20"})[0]
        bids, asks = book.get("bids", []), book.get("asks", [])
        if not bids or not asks:
            raise RuntimeError("missing_orderbook")
        funding = self.get("/api/v5/public/funding-rate", {"instId": symbol})[0]
        candles = {}
        for bar in bars:
            rows = list(reversed(self.get("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)})))
            if len(rows) < 51:
                raise RuntimeError(f"insufficient_{bar}_candles")
            rows = rows[:-1]  # exclude forming candle
            candles[bar] = tuple(NormalizedCandle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), True) for x in rows)
        return CandidateMarket(
            symbol=symbol, now_ms=now_ms, candles=candles,
            orderbook=NormalizedOrderBook(now_ms, float(bids[0][0]), float(asks[0][0]),
                sum(float(x[0]) * float(x[1]) for x in bids), sum(float(x[0]) * float(x[1]) for x in asks)),
            funding=NormalizedFunding(now_ms, float(funding["fundingRate"])),
            expected_move_pct=None, stop_loss_pct=None,
        )
