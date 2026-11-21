"""Append-only audit ledger for order intents and exchange write attempts."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _exchange_order_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("ordId", "orderId"):
        if payload.get(key):
            return str(payload[key])
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        for key in ("ordId", "orderId"):
            if data[0].get(key):
                return str(data[0][key])
    return ""


class EventLedger:
    """SQLite-backed shadow ledger. Recording failures never change trading behavior."""

    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    pos_side TEXT NOT NULL DEFAULT '',
                    order_type TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL DEFAULT '',
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    cl_ord_id TEXT,
                    exchange_order_id TEXT,
                    request_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS order_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    intent_id TEXT,
                    ts INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    operation TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    status_code TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    exchange_order_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_ts INTEGER NOT NULL,
                    completed_ts INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_count INTEGER NOT NULL DEFAULT 0,
                    position_count INTEGER NOT NULL DEFAULT 0,
                    difference_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS reconciliation_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id INTEGER,
                    ts INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    difference_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS exchange_order_state (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    cl_ord_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    pos_side TEXT NOT NULL DEFAULT '',
                    order_type TEXT NOT NULL DEFAULT '',
                    quantity TEXT NOT NULL DEFAULT '',
                    price TEXT NOT NULL DEFAULT '',
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    updated_ts INTEGER NOT NULL,
                    last_seen_ts INTEGER NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS exchange_position_state (
                    symbol TEXT NOT NULL,
                    pos_side TEXT NOT NULL,
                    quantity TEXT NOT NULL DEFAULT '0',
                    average_price TEXT NOT NULL DEFAULT '0',
                    mark_price TEXT NOT NULL DEFAULT '0',
                    notional TEXT NOT NULL DEFAULT '0',
                    unrealized_pnl TEXT NOT NULL DEFAULT '0',
                    margin TEXT NOT NULL DEFAULT '0',
                    updated_ts INTEGER NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(symbol, pos_side)
                );

                CREATE TABLE IF NOT EXISTS risk_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    updated_ts INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS risk_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    ts INTEGER NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS risk_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    ts INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    reason_code TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT '',
                    side TEXT NOT NULL DEFAULT '',
                    pos_side TEXT NOT NULL DEFAULT '',
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_order_intents_created
                    ON order_intents(created_ts);
                CREATE INDEX IF NOT EXISTS idx_order_intents_symbol_status
                    ON order_intents(symbol, status);
                CREATE INDEX IF NOT EXISTS idx_order_events_intent_ts
                    ON order_events(intent_id, ts);
                CREATE INDEX IF NOT EXISTS idx_order_events_type_ts
                    ON order_events(event_type, ts);
                CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_symbol_ts
                    ON reconciliation_runs(symbol, completed_ts);
                CREATE INDEX IF NOT EXISTS idx_reconciliation_events_symbol_ts
                    ON reconciliation_events(symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_exchange_orders_symbol_state
                    ON exchange_order_state(symbol, state);
                CREATE INDEX IF NOT EXISTS idx_risk_decisions_ts
                    ON risk_decisions(ts);
                CREATE INDEX IF NOT EXISTS idx_risk_decisions_symbol_ts
                    ON risk_decisions(symbol, ts);
                """
            )
            self._conn.commit()

    def create_order_intent(self, request: Dict[str, Any], source: str = "okx_client") -> str:
        intent_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO order_intents(
                    intent_id,created_ts,updated_ts,source,symbol,side,pos_side,
                    order_type,quantity,price,reduce_only,status,cl_ord_id,
                    exchange_order_id,request_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    intent_id,
                    now,
                    now,
                    str(source or ""),
                    str(request.get("instId") or ""),
                    str(request.get("side") or ""),
                    str(request.get("posSide") or ""),
                    str(request.get("ordType") or ""),
                    str(request.get("sz") or ""),
                    str(request.get("px") or ""),
                    int(str(request.get("reduceOnly", "false")).lower() == "true"),
                    "CREATED",
                    str(request.get("clOrdId") or "") or None,
                    None,
                    _json(request),
                ),
            )
            self._conn.commit()
        self.append_event(intent_id, "INTENT_CREATED", "place_order", payload=request)
        return intent_id

    def append_event(
        self,
        intent_id: Optional[str],
        event_type: str,
        operation: str = "",
        attempt: int = 0,
        status_code: str = "",
        message: str = "",
        payload: Any = None,
        intent_status: str = "",
    ) -> str:
        event_id = uuid.uuid4().hex
        now = int(time.time() * 1000)
        payload = {} if payload is None else payload
        order_id = _exchange_order_id(payload)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO order_events(
                    event_id,intent_id,ts,event_type,operation,attempt,status_code,
                    message,exchange_order_id,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    intent_id or None,
                    now,
                    str(event_type),
                    str(operation or ""),
                    int(attempt or 0),
                    str(status_code or ""),
                    str(message or ""),
                    order_id,
                    _json(payload),
                ),
            )
            if intent_id and intent_status:
                self._conn.execute(
                    """
                    UPDATE order_intents
                    SET status=?, updated_ts=?, exchange_order_id=COALESCE(NULLIF(?,''), exchange_order_id)
                    WHERE intent_id=?
                    """,
                    (str(intent_status), now, order_id, intent_id),
                )
            self._conn.commit()
        return event_id

    def record_local_rejection(
        self,
        symbol: str,
        side: str,
        pos_side: str,
        quantity: Any,
        price: Any,
        reason_code: str,
        source: str = "strategy_opening_guard",
        reduce_only: bool = False,
        payload: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Record a strategy-side block before it reaches the execution gateway.

        Local guards historically only emitted log lines. Creating an intent and
        a linked LOCAL_REJECT event gives those blocks the same correlation shape
        as RiskGate and exchange responses without submitting anything to OKX.
        """
        request = {
            "instId": str(symbol or ""),
            "side": str(side or ""),
            "posSide": str(pos_side or ""),
            "ordType": "local_guard",
            "sz": str(quantity or ""),
            "px": str(price or ""),
            "reduceOnly": "true" if reduce_only else "false",
        }
        intent_id = self.create_order_intent(request, source=source)
        event_payload = {
            "symbol": str(symbol or ""),
            "side": str(side or ""),
            "pos_side": str(pos_side or ""),
            "quantity": str(quantity or ""),
            "price": str(price or ""),
            "reason_code": str(reason_code or "local_reject"),
            **(payload or {}),
        }
        self.append_event(
            intent_id,
            "LOCAL_REJECT",
            operation="place_order",
            status_code="opening_guard",
            message=str(reason_code or "local_reject"),
            payload=event_payload,
            intent_status="LOCAL_REJECT",
        )
        return intent_id

    def get_unresolved_order_intents(self):
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT intent_id, symbol, cl_ord_id, status, request_json
                FROM order_intents
                WHERE status IN ('SUBMITTING', 'UNKNOWN')
                  AND COALESCE(cl_ord_id, '') <> ''
                ORDER BY created_ts
                """
            ).fetchall()
        result = []
        for intent_id, symbol, cl_ord_id, status, request_json in rows:
            try:
                request = json.loads(request_json)
            except (TypeError, ValueError):
                request = {}
            result.append(
                {
                    "intent_id": str(intent_id),
                    "symbol": str(symbol),
                    "cl_ord_id": str(cl_ord_id),
                    "status": str(status),
                    "request": request,
                }
            )
        return result

    def save_reconciliation_snapshot(self, symbol: str, orders, positions, started_ts: int = 0):
        now = int(time.time() * 1000)
        started_ts = int(started_ts or now)
        symbol = str(symbol)
        orders = list(orders or [])
        positions = list(positions or [])
        differences = []
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO reconciliation_runs(
                    started_ts,completed_ts,symbol,status,order_count,position_count,difference_count,message
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (started_ts, now, symbol, "SUCCESS", len(orders), len(positions), 0, ""),
            )
            run_id = int(cursor.lastrowid)
            existing_orders = {
                row[0]: row[1:]
                for row in self._conn.execute(
                    """
                    SELECT order_id,state,quantity,price,reduce_only,payload_json
                    FROM exchange_order_state WHERE symbol=?
                    """,
                    (symbol,),
                ).fetchall()
            }
            seen_order_ids = set()
            for order in orders:
                order_id = str(order.get("order_id") or "")
                if not order_id:
                    continue
                seen_order_ids.add(order_id)
                state = str(order.get("state") or "live")
                quantity = str(order.get("quantity") or "")
                price = str(order.get("price") or "")
                reduce_only = int(bool(order.get("reduce_only")))
                previous = existing_orders.get(order_id)
                if previous is None:
                    differences.append(("order", order_id, "ADDED", order))
                elif previous[:4] != (state, quantity, price, reduce_only):
                    differences.append(("order", order_id, "CHANGED", order))
                self._conn.execute(
                    """
                    INSERT INTO exchange_order_state(
                        order_id,symbol,cl_ord_id,state,side,pos_side,order_type,quantity,
                        price,reduce_only,updated_ts,last_seen_ts,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        symbol=excluded.symbol,cl_ord_id=excluded.cl_ord_id,state=excluded.state,
                        side=excluded.side,pos_side=excluded.pos_side,order_type=excluded.order_type,
                        quantity=excluded.quantity,price=excluded.price,reduce_only=excluded.reduce_only,
                        updated_ts=excluded.updated_ts,last_seen_ts=excluded.last_seen_ts,
                        payload_json=excluded.payload_json
                    """,
                    (
                        order_id,
                        symbol,
                        str(order.get("cl_ord_id") or ""),
                        state,
                        str(order.get("side") or ""),
                        str(order.get("pos_side") or ""),
                        str(order.get("order_type") or ""),
                        quantity,
                        price,
                        reduce_only,
                        now,
                        now,
                        _json(order.get("payload") or order),
                    ),
                )
            for order_id, previous in existing_orders.items():
                if order_id in seen_order_ids or previous[0] == "NOT_LIVE":
                    continue
                differences.append(
                    (
                        "order",
                        order_id,
                        "MISSING",
                        {"previous_state": previous[0]},
                    )
                )
                self._conn.execute(
                    """
                    UPDATE exchange_order_state
                    SET state='NOT_LIVE', updated_ts=?
                    WHERE order_id=?
                    """,
                    (now, order_id),
                )

            existing_positions = {
                row[0]: row[1:]
                for row in self._conn.execute(
                    """
                    SELECT pos_side,quantity,average_price,payload_json
                    FROM exchange_position_state WHERE symbol=?
                    """,
                    (symbol,),
                ).fetchall()
            }
            for position in positions:
                pos_side = str(position.get("pos_side") or "")
                if not pos_side:
                    continue
                quantity = str(position.get("quantity") or "0")
                average_price = str(position.get("average_price") or "0")
                previous = existing_positions.get(pos_side)
                if previous is None:
                    differences.append(("position", pos_side, "ADDED", position))
                elif previous[:2] != (quantity, average_price):
                    differences.append(("position", pos_side, "CHANGED", position))
                self._conn.execute(
                    """
                    INSERT INTO exchange_position_state(
                        symbol,pos_side,quantity,average_price,mark_price,notional,
                        unrealized_pnl,margin,updated_ts,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,pos_side) DO UPDATE SET
                        quantity=excluded.quantity,average_price=excluded.average_price,
                        mark_price=excluded.mark_price,notional=excluded.notional,
                        unrealized_pnl=excluded.unrealized_pnl,margin=excluded.margin,
                        updated_ts=excluded.updated_ts,payload_json=excluded.payload_json
                    """,
                    (
                        symbol,
                        pos_side,
                        quantity,
                        average_price,
                        str(position.get("mark_price") or "0"),
                        str(position.get("notional") or "0"),
                        str(position.get("unrealized_pnl") or "0"),
                        str(position.get("margin") or "0"),
                        now,
                        _json(position.get("payload") or position),
                    ),
                )

            for entity_type, entity_id, difference_type, payload in differences:
                self._conn.execute(
                    """
                    INSERT INTO reconciliation_events(
                        event_id,run_id,ts,symbol,entity_type,entity_id,difference_type,payload_json
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        uuid.uuid4().hex,
                        run_id,
                        now,
                        symbol,
                        entity_type,
                        entity_id,
                        difference_type,
                        _json(payload),
                    ),
                )
            self._conn.execute(
                "UPDATE reconciliation_runs SET difference_count=? WHERE id=?",
                (len(differences), run_id),
            )
            self._conn.commit()
        return {"run_id": run_id, "difference_count": len(differences)}

    def record_reconciliation_failure(self, symbol: str, message: str, started_ts: int = 0):
        now = int(time.time() * 1000)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO reconciliation_runs(
                    started_ts,completed_ts,symbol,status,order_count,position_count,difference_count,message
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (int(started_ts or now), now, str(symbol), "FAILED", 0, 0, 0, str(message)),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_or_create_risk_state(self):
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO risk_state(id,state,version,reason,actor,updated_ts)
                VALUES(1,'ACTIVE',1,'initial state','system',?)
                """,
                (now,),
            )
            row = self._conn.execute(
                "SELECT state,version,reason,actor,updated_ts FROM risk_state WHERE id=1"
            ).fetchone()
            self._conn.commit()
        return {
            "state": str(row[0]),
            "version": int(row[1]),
            "reason": str(row[2]),
            "actor": str(row[3]),
            "updated_ts": int(row[4]),
        }

    def get_risk_state(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT state,version,reason,actor,updated_ts FROM risk_state WHERE id=1"
            ).fetchone()
        if row is None:
            return self.get_or_create_risk_state()
        return {
            "state": str(row[0]),
            "version": int(row[1]),
            "reason": str(row[2]),
            "actor": str(row[3]),
            "updated_ts": int(row[4]),
        }

    def get_exchange_positions(self, symbol: str):
        """Return the latest reconciled position quantities for risk decisions."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT pos_side,quantity,average_price,mark_price,notional,unrealized_pnl
                FROM exchange_position_state WHERE symbol=?
                """,
                (str(symbol or ""),),
            ).fetchall()
        return [
            {
                "pos_side": str(row[0]),
                "quantity": str(row[1]),
                "average_price": str(row[2]),
                "mark_price": str(row[3]),
                "notional": str(row[4]),
                "unrealized_pnl": str(row[5]),
            }
            for row in rows
        ]

    def transition_risk_state(self, to_state: str, reason: str, actor: str):
        now = int(time.time() * 1000)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT state,version FROM risk_state WHERE id=1"
            ).fetchone()
            if row is None:
                from_state, version = "ACTIVE", 1
                self._conn.execute(
                    """
                    INSERT INTO risk_state(id,state,version,reason,actor,updated_ts)
                    VALUES(1,?,?,?,?,?)
                    """,
                    (from_state, version, "initial state", "system", now),
                )
            else:
                from_state, version = str(row[0]), int(row[1])
            if from_state == str(to_state):
                self._conn.commit()
                return {
                    "state": from_state,
                    "version": version,
                    "changed": False,
                }
            version += 1
            self._conn.execute(
                """
                UPDATE risk_state SET state=?,version=?,reason=?,actor=?,updated_ts=? WHERE id=1
                """,
                (str(to_state), version, str(reason), str(actor), now),
            )
            self._conn.execute(
                """
                INSERT INTO risk_transitions(
                    event_id,ts,from_state,to_state,reason,actor,version
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex,
                    now,
                    from_state,
                    str(to_state),
                    str(reason),
                    str(actor),
                    version,
                ),
            )
            self._conn.commit()
        return {"state": str(to_state), "version": version, "changed": True}

    def record_risk_decision(self, decision: Dict[str, Any]):
        payload = dict(decision.get("payload") or {})
        shadow = decision.get("shadow")
        if isinstance(shadow, dict):
            # Keep the final policy evidence with the execution decision. This
            # makes the narrowly scoped inventory-repair corridor auditable
            # without creating a second, unsynchronised event stream.
            payload["risk_shadow"] = shadow
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO risk_decisions(
                    event_id,ts,state,allowed,reason_code,symbol,side,pos_side,
                    reduce_only,payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex,
                    int(time.time() * 1000),
                    str(decision.get("state") or ""),
                    int(bool(decision.get("allowed"))),
                    str(decision.get("reason_code") or ""),
                    str(decision.get("symbol") or ""),
                    str(decision.get("side") or ""),
                    str(decision.get("pos_side") or ""),
                    int(bool(decision.get("reduce_only"))),
                    _json(payload),
                ),
            )
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()
