#!/usr/bin/env python3
"""Lightweight live-order state tracker.

Tracks exchange live orders across sync cycles without making trading decisions.
Inspired by Hummingbot ClientOrderTracker, but intentionally minimal:
active -> cached -> lost -> final-like forget.
"""
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, Tuple


def _order_id(order: dict) -> str:
    return str(order.get("ordId") or order.get("orderId") or order.get("clOrdId") or "").strip()


def _signature(order: dict) -> Tuple[str, str, str, str, str]:
    return (
        str(order.get("side") or ""),
        str(order.get("posSide") or ""),
        str(order.get("px") or ""),
        str(order.get("sz") or order.get("size") or ""),
        str(order.get("reduceOnly") or ""),
    )


@dataclass
class TrackedOrder:
    order_id: str
    symbol: str = ""
    side: str = ""
    pos_side: str = ""
    price: float = 0.0
    size: float = 0.0
    reduce_only: bool = False
    state: str = "active"
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    missing_count: int = 0
    signature: Tuple[str, str, str, str, str] = field(default_factory=tuple)


class OrderTracker:
    def __init__(self, symbol: str, lost_after: int = 3, forget_after: int = 12):
        self.symbol = symbol
        self.lost_after = max(1, int(lost_after))
        self.forget_after = max(self.lost_after + 1, int(forget_after))
        self._orders: Dict[str, TrackedOrder] = {}

    def update_live_orders(self, orders: Iterable[dict]) -> dict:
        now = time.time()
        seen = set()
        added = restored = changed = lost = forgotten = 0
        for order in orders or []:
            oid = _order_id(order)
            if not oid:
                continue
            seen.add(oid)
            sig = _signature(order)
            item = self._orders.get(oid)
            if item is None:
                self._orders[oid] = TrackedOrder(
                    order_id=oid,
                    symbol=str(order.get("instId") or self.symbol),
                    side=str(order.get("side") or ""),
                    pos_side=str(order.get("posSide") or ""),
                    price=float(order.get("px") or 0),
                    size=float(order.get("sz") or order.get("size") or 0),
                    reduce_only=str(order.get("reduceOnly") or "").lower() == "true",
                    state="active",
                    first_seen=now,
                    last_seen=now,
                    missing_count=0,
                    signature=sig,
                )
                added += 1
                continue
            if item.state in ("cached", "lost"):
                restored += 1
            if item.signature and item.signature != sig:
                changed += 1
            item.state = "active"
            item.last_seen = now
            item.missing_count = 0
            item.signature = sig
            item.side = str(order.get("side") or item.side)
            item.pos_side = str(order.get("posSide") or item.pos_side)
            item.price = float(order.get("px") or item.price or 0)
            item.size = float(order.get("sz") or order.get("size") or item.size or 0)
            item.reduce_only = str(order.get("reduceOnly") or item.reduce_only).lower() == "true"

        for oid, item in list(self._orders.items()):
            if oid in seen:
                continue
            item.missing_count += 1
            item.state = "lost" if item.missing_count >= self.lost_after else "cached"
            if item.missing_count == self.lost_after:
                lost += 1
            if item.missing_count >= self.forget_after:
                self._orders.pop(oid, None)
                forgotten += 1

        active = sum(1 for o in self._orders.values() if o.state == "active")
        cached = sum(1 for o in self._orders.values() if o.state == "cached")
        lost_total = sum(1 for o in self._orders.values() if o.state == "lost")
        return {
            "active": active,
            "cached": cached,
            "lost": lost_total,
            "added": added,
            "restored": restored,
            "changed": changed,
            "lost_new": lost,
            "forgotten": forgotten,
            "total": len(self._orders),
        }

    def summary(self) -> dict:
        now = time.time()
        active_orders = [o for o in self._orders.values() if o.state == "active"]
        cached_orders = [o for o in self._orders.values() if o.state == "cached"]
        lost_orders = [o for o in self._orders.values() if o.state == "lost"]
        ages = [max(0.0, now - o.first_seen) for o in active_orders]
        reduce_orders = [o for o in active_orders if o.reduce_only]
        grid_orders = [o for o in active_orders if not o.reduce_only]
        by_side = {
            "long": [o for o in active_orders if o.pos_side == "long"],
            "short": [o for o in active_orders if o.pos_side == "short"],
        }
        def avg(values):
            return sum(values) / len(values) if values else 0.0
        def max_age(items):
            return max((max(0.0, now - o.first_seen) for o in items), default=0.0)
        return {
            "active": len(active_orders),
            "cached": len(cached_orders),
            "lost": len(lost_orders),
            "total": len(self._orders),
            "active_avg_age_sec": round(avg(ages), 1),
            "active_max_age_sec": round(max(ages), 1) if ages else 0.0,
            "grid_count": len(grid_orders),
            "grid_max_age_sec": round(max_age(grid_orders), 1),
            "tp_count": len(reduce_orders),
            "tp_max_age_sec": round(max_age(reduce_orders), 1),
            "long_active": len(by_side["long"]),
            "long_max_age_sec": round(max_age(by_side["long"]), 1),
            "short_active": len(by_side["short"]),
            "short_max_age_sec": round(max_age(by_side["short"]), 1),
        }
