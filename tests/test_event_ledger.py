import sqlite3
import threading
from types import SimpleNamespace

from api.okx_client import OKXClient
from execution.event_ledger import EventLedger


class LogStub:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def make_client(ledger, place_order, get_order=None, retries=1):
    client = OKXClient.__new__(OKXClient)
    client.log = LogStub()
    client._event_ledger = ledger
    client._execution_guard = None
    client._trade = SimpleNamespace(
        place_order=place_order,
        get_order=get_order or (lambda **_kwargs: {"code": "51603", "data": []}),
    )
    client._lock = threading.Lock()
    client._call_times = []
    client._retries = retries
    client._retry_delay = 0
    client._rate_limit_window = 2.0
    client._rate_limit_max_calls = 100
    client._rate_backoff_until = 0.0
    client._rate_limit_failures = 0
    client._clock = lambda: 0.0
    client._sleep = lambda _seconds: None
    client._unknown_orders = {}
    client._unknown_orders_lock = threading.Lock()
    return client


def read_ledger(db_path):
    with sqlite3.connect(db_path) as conn:
        intent = conn.execute(
            "SELECT intent_id, status, exchange_order_id, cl_ord_id FROM order_intents"
        ).fetchone()
        events = conn.execute(
            "SELECT event_type, attempt FROM order_events ORDER BY id"
        ).fetchall()
    return intent, events


def test_successful_order_records_complete_event_chain(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))

    requests = []

    def place_order(**kwargs):
        requests.append(kwargs)
        return {
            "code": "0",
            "data": [{"ordId": "okx-123", "sCode": "0", "sMsg": ""}],
        }

    client = make_client(ledger, place_order)

    result = client.place_order(
        "TEST-USDT-SWAP", "buy", "1", ord_type="limit", px="10", pos_side="long"
    )

    intent, events = read_ledger(db_path)
    ledger.close()
    assert result["ordId"] == "okx-123"
    assert intent[1:3] == ("ACCEPTED", "okx-123")
    assert intent[3].startswith("oq")
    assert len(intent[3]) == 32
    assert intent[3].isalnum()
    assert requests[0]["clOrdId"] == intent[3]
    assert events == [
        ("INTENT_CREATED", 0),
        ("API_ATTEMPT", 1),
        ("API_RESULT", 1),
        ("ORDER_ACCEPTED", 0),
    ]


def test_final_network_timeout_marks_order_unknown(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))

    place_calls = []
    lookup_calls = []

    def place_order(**kwargs):
        place_calls.append(kwargs)
        raise TimeoutError("exchange response timed out")

    def get_order(**kwargs):
        lookup_calls.append(kwargs)
        return {"code": "51603", "msg": "Order does not exist", "data": []}

    client = make_client(ledger, place_order, get_order=get_order, retries=3)

    result = client.place_order("TEST-USDT-SWAP", "sell", "2", pos_side="short")

    intent, events = read_ledger(db_path)
    ledger.close()
    assert intent[1] == "UNKNOWN"
    assert result["sCode"] == "order_unknown"
    assert result["clOrdId"] == intent[3]
    assert len(place_calls) == 1
    assert len(lookup_calls) == 3
    assert all(call["clOrdId"] == intent[3] for call in lookup_calls)
    assert events == [
        ("INTENT_CREATED", 0),
        ("API_ATTEMPT", 1),
        ("API_EXCEPTION", 1),
        ("ORDER_LOOKUP_ATTEMPT", 1),
        ("ORDER_LOOKUP_RESULT", 1),
        ("ORDER_LOOKUP_ATTEMPT", 2),
        ("ORDER_LOOKUP_RESULT", 2),
        ("ORDER_LOOKUP_ATTEMPT", 3),
        ("ORDER_LOOKUP_RESULT", 3),
        ("ORDER_UNKNOWN", 0),
    ]


def test_network_timeout_recovers_order_by_client_order_id(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    submitted = []

    def place_order(**kwargs):
        submitted.append(kwargs)
        raise TimeoutError("response lost after submit")

    def get_order(**kwargs):
        assert kwargs["clOrdId"] == submitted[0]["clOrdId"]
        return {
            "code": "0",
            "data": [{"ordId": "recovered-123", "clOrdId": kwargs["clOrdId"], "state": "live"}],
        }

    client = make_client(ledger, place_order, get_order=get_order, retries=3)

    result = client.place_order("TEST-USDT-SWAP", "buy", "1", pos_side="long")

    intent, events = read_ledger(db_path)
    ledger.close()
    assert len(submitted) == 1
    assert result["ordId"] == "recovered-123"
    assert result["recovered"] is True
    assert intent[1:3] == ("ACCEPTED", "recovered-123")
    assert events[-1] == ("ORDER_RECOVERED", 0)


def test_unknown_opening_order_blocks_resubmit_but_allows_reduce_only(tmp_path):
    ledger = EventLedger(str(tmp_path / "trading.db"))
    place_calls = []

    def place_order(**kwargs):
        place_calls.append(kwargs)
        if len(place_calls) == 1:
            raise TimeoutError("unknown opening order")
        return {"code": "0", "data": [{"ordId": "reduce-123", "sCode": "0"}]}

    client = make_client(ledger, place_order, retries=3)

    first = client.place_order("TEST-USDT-SWAP", "buy", "1", pos_side="long")
    second = client.place_order("TEST-USDT-SWAP", "buy", "1", pos_side="long")
    reduction = client.place_order(
        "TEST-USDT-SWAP", "sell", "1", pos_side="long", reduce_only=True
    )

    ledger.close()
    assert first["sCode"] == "order_unknown"
    assert second["sCode"] == "order_unknown"
    assert second["guard_blocked"] is True
    assert len(place_calls) == 2
    assert reduction["ordId"] == "reduce-123"
    assert place_calls[-1]["reduceOnly"] == "true"


def test_ambiguous_api_response_queries_instead_of_resubmitting(tmp_path):
    ledger = EventLedger(str(tmp_path / "trading.db"))
    place_calls = []

    def place_order(**kwargs):
        place_calls.append(kwargs)
        return {"code": "1", "msg": "All operations failed", "data": []}

    def get_order(**kwargs):
        return {
            "code": "0",
            "data": [{"ordId": "ambiguous-123", "clOrdId": kwargs["clOrdId"]}],
        }

    client = make_client(ledger, place_order, get_order=get_order, retries=3)

    result = client.place_order("TEST-USDT-SWAP", "buy", "1", pos_side="long")

    ledger.close()
    assert len(place_calls) == 1
    assert result["ordId"] == "ambiguous-123"
    assert result["recovered"] is True


def test_unresolved_order_is_loaded_from_ledger_after_restart(tmp_path):
    ledger = EventLedger(str(tmp_path / "trading.db"))
    cl_ord_id = "oq" + "a" * 30
    intent_id = ledger.create_order_intent(
        {
            "instId": "TEST-USDT-SWAP",
            "side": "buy",
            "ordType": "limit",
            "sz": "1",
            "px": "10",
            "clOrdId": cl_ord_id,
        }
    )
    ledger.append_event(
        intent_id,
        "ORDER_UNKNOWN",
        operation="place_order",
        intent_status="UNKNOWN",
    )
    client = OKXClient.__new__(OKXClient)
    client.log = LogStub()
    client._event_ledger = ledger
    client._unknown_orders = {}
    client._unknown_orders_lock = threading.Lock()

    client._load_unresolved_orders()

    ledger.close()
    assert client._unknown_orders[cl_ord_id] == {
        "cl_ord_id": cl_ord_id,
        "symbol": "TEST-USDT-SWAP",
        "intent_id": intent_id,
    }


def test_ledger_failure_does_not_block_successful_order():
    class BrokenLedger:
        def create_order_intent(self, _request):
            raise sqlite3.OperationalError("disk unavailable")

        def append_event(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("disk unavailable")

    calls = []

    def place_order(**kwargs):
        calls.append(kwargs)
        return {"code": "0", "data": [{"ordId": "okx-456", "sCode": "0"}]}

    client = make_client(BrokenLedger(), place_order)

    result = client.place_order("TEST-USDT-SWAP", "buy", "1", pos_side="long")

    assert result["ordId"] == "okx-456"
    assert len(calls) == 1
