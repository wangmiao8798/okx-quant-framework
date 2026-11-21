import sqlite3
from types import SimpleNamespace

from execution.event_ledger import EventLedger
from execution.reconciler import ExchangeReconciler


class FakeClient:
    def __init__(self, orders, positions):
        self.orders = orders
        self.positions = positions
        self.fail_orders = False
        self._account = SimpleNamespace(get_positions=object())

    def get_order_list(self, _symbol):
        if self.fail_orders:
            raise RuntimeError("order state unavailable")
        return list(self.orders)

    def _call(self, _method, **_kwargs):
        return {"code": "0", "data": list(self.positions)}


def test_reconciler_builds_and_updates_exchange_projection(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    client = FakeClient(
        orders=[
            {
                "ordId": "order-1",
                "clOrdId": "client-1",
                "state": "live",
                "side": "buy",
                "posSide": "long",
                "ordType": "limit",
                "sz": "1",
                "px": "100",
                "reduceOnly": "false",
            }
        ],
        positions=[{"posSide": "long", "pos": "1", "avgPx": "100", "markPx": "101"}],
    )
    reconciler = ExchangeReconciler(client, ledger, ["TEST-USDT-SWAP"])

    first = reconciler.reconcile_all()
    second = reconciler.reconcile_all()
    client.orders = []
    client.positions = [{"posSide": "long", "pos": "2", "avgPx": "101"}]
    third = reconciler.reconcile_all()

    with sqlite3.connect(db_path) as conn:
        order = conn.execute(
            "SELECT state,cl_ord_id FROM exchange_order_state WHERE order_id='order-1'"
        ).fetchone()
        positions = conn.execute(
            "SELECT pos_side,quantity,average_price FROM exchange_position_state ORDER BY pos_side"
        ).fetchall()
        events = conn.execute(
            "SELECT entity_type,difference_type FROM reconciliation_events ORDER BY id"
        ).fetchall()
        runs = conn.execute(
            "SELECT status,difference_count FROM reconciliation_runs ORDER BY id"
        ).fetchall()
    ledger.close()

    assert first["TEST-USDT-SWAP"]["difference_count"] == 3
    assert second["TEST-USDT-SWAP"]["difference_count"] == 0
    assert third["TEST-USDT-SWAP"]["difference_count"] == 2
    assert reconciler.ready is True
    assert order == ("NOT_LIVE", "client-1")
    assert positions == [("long", "2", "101"), ("short", "0", "0")]
    assert events == [
        ("order", "ADDED"),
        ("position", "ADDED"),
        ("position", "ADDED"),
        ("order", "MISSING"),
        ("position", "CHANGED"),
    ]
    assert runs == [("SUCCESS", 3), ("SUCCESS", 0), ("SUCCESS", 2)]


def test_reconciler_failure_is_recorded_without_erasing_projection(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    client = FakeClient(
        orders=[{"ordId": "order-1", "state": "live", "sz": "1", "px": "100"}],
        positions=[],
    )
    reconciler = ExchangeReconciler(client, ledger, ["TEST-USDT-SWAP"])
    reconciler.reconcile_all()
    client.fail_orders = True

    result = reconciler.reconcile_all()

    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT state FROM exchange_order_state WHERE order_id='order-1'"
        ).fetchone()[0]
        last_run = conn.execute(
            "SELECT status,message FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    ledger.close()

    assert result["TEST-USDT-SWAP"]["ok"] is False
    assert reconciler.ready is False
    assert state == "live"
    assert last_run[0] == "FAILED"
    assert "order state unavailable" in last_run[1]
