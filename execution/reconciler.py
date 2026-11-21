"""Read-only exchange state reconciler and SQLite projection updater."""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List


def _truthy(value: Any) -> bool:
    return str(value).lower() == "true" or value is True


class ExchangeReconciler:
    def __init__(self, client, event_ledger, symbols: Iterable[str], log=None, interval: float = 30.0):
        self.client = client
        self.event_ledger = event_ledger
        self.symbols = tuple(sorted({str(symbol) for symbol in symbols if symbol}))
        self.log = log
        self.interval = max(10.0, float(interval))
        self.ready = False
        self.last_run = 0.0
        self.last_success = 0.0
        self.last_error = ""
        self.results: Dict[str, Dict[str, Any]] = {}

    def is_due(self) -> bool:
        return time.monotonic() - self.last_run >= self.interval

    def _fetch_positions(self, symbol: str) -> List[Dict[str, Any]]:
        response = self.client._call(self.client._account.get_positions, instId=symbol)
        if (
            not isinstance(response, dict)
            or str(response.get("code", "")) != "0"
            or not isinstance(response.get("data"), list)
        ):
            raise RuntimeError(f"position query failed: {response}")
        rows = response.get("data") or []
        by_side = {
            str(row.get("posSide") or "").lower(): row
            for row in rows
            if str(row.get("posSide") or "").lower() in {"long", "short"}
        }
        positions = []
        for side in ("long", "short"):
            row = by_side.get(side, {})
            positions.append(
                {
                    "pos_side": side,
                    "quantity": str(row.get("pos") or "0"),
                    "average_price": str(row.get("avgPx") or "0"),
                    "mark_price": str(row.get("markPx") or "0"),
                    "notional": str(row.get("notionalUsd") or row.get("notional") or "0"),
                    "unrealized_pnl": str(row.get("upl") or "0"),
                    "margin": str(row.get("margin") or "0"),
                    "payload": row,
                }
            )
        return positions

    @staticmethod
    def _normalize_orders(rows) -> List[Dict[str, Any]]:
        result = []
        for row in rows or []:
            order_id = str(row.get("ordId") or row.get("orderId") or "")
            if not order_id:
                continue
            result.append(
                {
                    "order_id": order_id,
                    "cl_ord_id": str(row.get("clOrdId") or ""),
                    "state": str(row.get("state") or "live"),
                    "side": str(row.get("side") or ""),
                    "pos_side": str(row.get("posSide") or ""),
                    "order_type": str(row.get("ordType") or ""),
                    "quantity": str(row.get("sz") or ""),
                    "price": str(row.get("px") or ""),
                    "reduce_only": _truthy(row.get("reduceOnly")),
                    "payload": row,
                }
            )
        return result

    def reconcile_symbol(self, symbol: str) -> Dict[str, Any]:
        started_ts = int(time.time() * 1000)
        try:
            orders = self._normalize_orders(self.client.get_order_list(symbol))
            positions = self._fetch_positions(symbol)
            result = self.event_ledger.save_reconciliation_snapshot(
                symbol, orders, positions, started_ts=started_ts
            )
            result.update(
                {
                    "ok": True,
                    "symbol": symbol,
                    "order_count": len(orders),
                    "position_count": len(positions),
                }
            )
            return result
        except Exception as exc:
            try:
                self.event_ledger.record_reconciliation_failure(
                    symbol, str(exc), started_ts=started_ts
                )
            except Exception:
                pass
            return {"ok": False, "symbol": symbol, "error": str(exc)}

    def reconcile_all(self) -> Dict[str, Dict[str, Any]]:
        self.last_run = time.monotonic()
        results = {symbol: self.reconcile_symbol(symbol) for symbol in self.symbols}
        self.results = results
        self.ready = bool(results) and all(result.get("ok") for result in results.values())
        if self.ready:
            self.last_success = time.time()
            self.last_error = ""
        else:
            failures = [
                f"{symbol}: {result.get('error', 'unknown error')}"
                for symbol, result in results.items()
                if not result.get("ok")
            ]
            self.last_error = "; ".join(failures)
        if self.log:
            if self.ready:
                changed = sum(int(result.get("difference_count", 0)) for result in results.values())
                self.log.info(
                    f"[Reconciler] snapshot ready symbols={len(results)} differences={changed}"
                )
            else:
                self.log.warning(f"[Reconciler] snapshot incomplete: {self.last_error}")
        return results

    def get_status(self) -> Dict[str, Any]:
        return {
            "ready": self.ready,
            "symbols": list(self.symbols),
            "last_success": self.last_success,
            "last_error": self.last_error,
            "results": self.results,
        }
