import json
import sqlite3
import time
import inspect
import pytest
from types import SimpleNamespace

from execution.event_ledger import EventLedger
from execution.gateway import ExecutionGateway
from core.engine import MainEngine
from risk.policy import RiskPolicy
from risk.risk_gate import RiskGate


class ReconcilerStub:
    def __init__(self, ready=True, last_success=None):
        self.ready = ready
        self.last_success = time.time() if last_success is None else last_success

    def get_status(self):
        return {"ready": self.ready, "last_success": self.last_success}


class ClientStub:
    def __init__(self):
        self.calls = []

    def place_order(self, symbol, side, quantity, **kwargs):
        self.calls.append((symbol, side, quantity, kwargs))
        return {"ordId": f"order-{len(self.calls)}"}


def _context(symbol="TEST-USDT-SWAP", long_qty=0.0, short_qty=0.0, margin_load=0.10):
    return {
        "context_version": 1,
        "generated_at": time.time(),
        "symbol": symbol,
        "positions": {
            "long_qty": long_qty,
            "short_qty": short_qty,
            "max_net_exposure": 0.0,
            "max_net_notional_usdt": 10000.0,
        },
        "account": {
            "margin_load_ratio": margin_load,
            "account_margin_soft_limit": 0.45,
            "account_margin_hard_limit": 0.55,
            "account_margin_resume_limit": 0.50,
            "account_margin_scope": "test_account",
            "hard_margin_ratio": 0.55,
            "paired_net_neutral_max_margin_ratio": 0.48,
            "mgn_ratio_open_limit": 5.0,
            "risk_context_max_age_sec": 30.0,
            "mark_price": 100.0,
            "contract_value": 1.0,
        },
        "account_risk": {"usdt": {"mgnRatio": "30"}},
    }


def test_active_gate_allows_opening_order_and_records_decision(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    gate = RiskGate(ledger, ReconcilerStub())
    client = ClientStub()
    gateway = ExecutionGateway(client, risk_gate=gate)

    result = gateway.place_order(
        "TEST-USDT-SWAP", "buy", "1", pos_side="long", risk_context=_context()
    )

    with sqlite3.connect(db_path) as conn:
        decision = conn.execute(
            "SELECT state,allowed,reason_code,symbol,reduce_only FROM risk_decisions"
        ).fetchone()
    ledger.close()
    assert result["ordId"] == "order-1"
    assert decision == ("ACTIVE", 1, "active", "TEST-USDT-SWAP", 0)


def test_stale_reconciler_blocks_opening_but_allows_reduction(tmp_path):
    ledger = EventLedger(str(tmp_path / "trading.db"))
    reconciler = ReconcilerStub(last_success=time.time() - 120)
    gate = RiskGate(ledger, reconciler, max_reconciler_age=90)
    client = ClientStub()
    gateway = ExecutionGateway(client, risk_gate=gate)

    opening = gateway.place_order(
        "TEST-USDT-SWAP", "buy", "1", pos_side="long", risk_context=_context()
    )
    reduction = gateway.place_order(
        "TEST-USDT-SWAP", "sell", "1", pos_side="long", reduce_only=True
    )

    ledger.close()
    assert opening == {
        "sCode": "risk_gate",
        "sMsg": "reconciler_stale",
        "risk_state": "ACTIVE",
        "guard_blocked": True,
    }
    assert reduction["ordId"] == "order-1"
    assert len(client.calls) == 1
    assert client.calls[0][3]["reduce_only"] is True


def test_persistent_reduce_only_state_survives_gate_restart(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    first_gate = RiskGate(ledger, ReconcilerStub())

    transition = first_gate.set_state("REDUCE_ONLY", "operator test", actor="test")
    second_gate = RiskGate(ledger, ReconcilerStub())
    client = ClientStub()
    gateway = ExecutionGateway(client, risk_gate=second_gate)
    opening = gateway.place_order(
        "TEST-USDT-SWAP", "buy", "1", pos_side="long", risk_context=_context()
    )
    reduction = gateway.place_order(
        "TEST-USDT-SWAP", "sell", "1", pos_side="long", reduce_only=True
    )

    with sqlite3.connect(db_path) as conn:
        state = conn.execute("SELECT state,version FROM risk_state WHERE id=1").fetchone()
        transitions = conn.execute(
            "SELECT from_state,to_state,reason,actor FROM risk_transitions"
        ).fetchall()
    ledger.close()
    assert transition["changed"] is True
    assert state == ("REDUCE_ONLY", 2)
    assert transitions == [("ACTIVE", "REDUCE_ONLY", "operator test", "test")]
    assert opening["sMsg"] == "risk_state_reduce_only"
    assert reduction["ordId"] == "order-1"


def test_inventory_repair_allows_only_orders_that_reduce_net_exposure(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    ledger.save_reconciliation_snapshot(
        "TEST-USDT-SWAP",
        [],
        [
            {"pos_side": "long", "quantity": "10", "average_price": "100", "mark_price": "100"},
            {"pos_side": "short", "quantity": "8", "average_price": "100", "mark_price": "100"},
        ],
    )
    gate = RiskGate(ledger, ReconcilerStub())
    gate.set_state("INVENTORY_REPAIR", "inventory test", actor="test")
    client = ClientStub()
    gateway = ExecutionGateway(client, risk_gate=gate)

    repair_context = _context(long_qty=10.0, short_qty=8.0)
    repair_long = gateway.place_order(
        "TEST-USDT-SWAP", "sell", "1", pos_side="long",
        intent_type="inventory_repair", risk_context=repair_context,
    )
    repair_short = gateway.place_order(
        "TEST-USDT-SWAP", "sell", "1", pos_side="short",
        intent_type="inventory_repair", risk_context=repair_context,
    )
    wrong_short = gateway.place_order(
        "TEST-USDT-SWAP", "buy", "1", pos_side="short",
        intent_type="inventory_repair", risk_context=repair_context,
    )
    worsens = gateway.place_order(
        "TEST-USDT-SWAP", "buy", "1", pos_side="long",
        intent_type="inventory_repair", risk_context=repair_context,
    )

    ledger.close()
    assert repair_long["ordId"] == "order-1"
    assert repair_short["ordId"] == "order-2"
    assert wrong_short["sMsg"] == "inventory_repair_worsens_net_exposure"
    assert worsens["sMsg"] == "inventory_repair_worsens_net_exposure"


def test_gateway_audits_cancel_leverage_and_close_mutations(tmp_path):
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    gate = RiskGate(ledger, ReconcilerStub())

    class MutationClient(ClientStub):
        def __init__(self):
            super().__init__()
            self._event_ledger = ledger
            self._trade = SimpleNamespace(cancel_order=object())

        def _call(self, *_args, **_kwargs):
            return {"code": "0", "msg": ""}

        def set_leverage(self, *_args, **_kwargs):
            return {"code": "0"}

        def close_all(self, *_args, **_kwargs):
            return {"code": "0"}

    client = MutationClient()
    gateway = ExecutionGateway(client, risk_gate=gate)
    assert gateway.cancel_order("TEST-USDT-SWAP", "order-1") is True
    assert gateway.set_leverage("TEST-USDT-SWAP", 2)["code"] == "0"
    assert gateway.close_all("TEST-USDT-SWAP")["code"] == "0"

    with sqlite3.connect(db_path) as conn:
        operations = conn.execute(
            "SELECT DISTINCT operation FROM order_events WHERE event_type IN ('MUTATION_REQUESTED','MUTATION_RESULT')"
        ).fetchall()
    ledger.close()
    assert {row[0] for row in operations} == {"cancel_order", "set_leverage", "close_all"}


def _margin_context(margin_load=0.47, paired_limit=0.48):
    return {
        "context_version": 1,
        "generated_at": time.time(),
        "symbol": "TEST-USDT-SWAP",
        "positions": {
            "long_qty": 10.0,
            "short_qty": 20.0,
            "max_net_exposure": 0.0,
            "max_net_notional_usdt": 10000.0,
        },
        "account": {
            "margin_load_ratio": margin_load,
            "account_margin_soft_limit": 0.45,
            "account_margin_hard_limit": 0.55,
            "account_margin_resume_limit": 0.50,
            "account_margin_scope": "test_account",
            "hard_margin_ratio": 0.55,
            "paired_net_neutral_max_margin_ratio": paired_limit,
            "mgn_ratio_open_limit": 5.0,
            "risk_context_max_age_sec": 30.0,
            "mark_price": 100.0,
            "contract_value": 1.0,
        },
        "account_risk": {"usdt": {"mgnRatio": "30"}},
    }


def _crv_hard_override_context(margin_load=0.6177, mgn_ratio=20.0):
    context = _margin_context(margin_load)
    context["symbol"] = "CRV-USDT-SWAP"
    context["positions"].update({
        "long_qty": 0.0,
        "short_qty": 2775.0,
        "max_net_notional_usdt": 150.0,
    })
    equity = 1838.5
    context["account"].update({
        "margin_load_ratio": margin_load,
        "account_margin_hard_limit": 0.60,
        "account_margin_resume_limit": 0.599,
        "account_margin_scope": "crv_override_test",
        "hard_margin_ratio": 0.60,
        "mark_price": 0.2684,
        "contract_value": 1.0,
        "leverage": 5.0,
        "margin_buffer_ratio": 0.10,
        "equity_usdt": equity,
        "frozen_margin_usdt": equity * margin_load,
        "account_budget_enabled": True,
        "budget_remaining_margin": 415.0,
        "inventory_repair_hard_override_enabled": True,
        "inventory_repair_hard_override_symbol": "CRV-USDT-SWAP",
        "inventory_repair_hard_override_margin_ratio": 0.63,
        "inventory_repair_hard_override_mgn_ratio_min": 12.0,
        "inventory_repair_hard_override_max_step_notional_usdt": 8.0,
    })
    context["account_risk"] = {"usdt": {"mgnRatio": str(mgn_ratio)}}
    return context


def test_crv_hard_margin_override_is_audited_and_never_bypasses_gateway(tmp_path):
    RiskPolicy._hard_margin_latches.clear()
    db_path = tmp_path / "trading.db"
    ledger = EventLedger(str(db_path))
    client = ClientStub()
    gateway = ExecutionGateway(client, risk_gate=RiskGate(ledger, ReconcilerStub()))

    result = gateway.place_order(
        "CRV-USDT-SWAP",
        "buy",
        "27",
        pos_side="long",
        inventory_repair=True,
        intent_type="inventory_repair",
        risk_context=_crv_hard_override_context(),
    )

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT allowed,reason_code,payload_json FROM risk_decisions"
        ).fetchone()
    ledger.close()
    payload = json.loads(row[2])
    metrics = payload["risk_shadow"]["metrics"]
    assert result["ordId"] == "order-1"
    assert row[:2] == (1, "inventory_repair_hard_margin_override_allowed")
    assert metrics["inventory_repair_hard_override_applied"] is True
    assert metrics["inventory_repair_projected_margin_load"] < 0.63
    assert metrics["required_margin"] < 2.0


def test_crv_hard_margin_override_rejects_normal_grid_and_wrong_repair_cases():
    reconciler = {"ready": True, "last_success": time.time()}

    RiskPolicy._hard_margin_latches.clear()
    normal = RiskPolicy.evaluate_order(
        "ACTIVE", "CRV-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "27", "px": "0.2684"},
        reconciler_status=reconciler,
        context=_crv_hard_override_context(),
    )
    assert normal["reason_code"] == "account_hard_margin_limit"

    RiskPolicy._hard_margin_latches.clear()
    low_mgn = RiskPolicy.evaluate_order(
        "ACTIVE", "CRV-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "27", "px": "0.2684", "inventory_repair": True},
        reconciler_status=reconciler,
        context=_crv_hard_override_context(mgn_ratio=11.9),
    )
    assert low_mgn["allowed"] is False
    assert low_mgn["shadow"]["metrics"]["inventory_repair_hard_override_applied"] is False
    assert low_mgn["shadow"]["metrics"]["inventory_repair_hard_override_reason"] == "mgn_ratio_below_override_min"

    RiskPolicy._hard_margin_latches.clear()
    wrong_direction = RiskPolicy.evaluate_order(
        "ACTIVE", "CRV-USDT-SWAP", "buy", "short", False,
        payload={"quantity": "27", "px": "0.2684", "inventory_repair": True},
        reconciler_status=reconciler,
        context=_crv_hard_override_context(),
    )
    assert wrong_direction["reason_code"] == "account_hard_margin_limit"

    RiskPolicy._hard_margin_latches.clear()
    projected_over_cap = RiskPolicy.evaluate_order(
        "ACTIVE", "CRV-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "27", "px": "0.2684", "inventory_repair": True},
        reconciler_status=reconciler,
        context=_crv_hard_override_context(0.6295),
    )
    assert projected_over_cap["reason_code"] == "account_hard_margin_limit"
    assert projected_over_cap["shadow"]["metrics"]["inventory_repair_hard_override_reason"] == "projected_margin_limit"

    RiskPolicy._hard_margin_latches.clear()
    other_symbol_context = _crv_hard_override_context()
    other_symbol_context["symbol"] = "DRAM-USDT-SWAP"
    other_symbol = RiskPolicy.evaluate_order(
        "ACTIVE", "DRAM-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "27", "px": "0.2684", "inventory_repair": True},
        reconciler_status=reconciler,
        context=other_symbol_context,
    )
    assert other_symbol["reason_code"] == "account_hard_margin_limit"


def test_soft_margin_throttles_but_does_not_block_normal_open():
    RiskPolicy._hard_margin_latches.clear()
    reconciler = {"ready": True, "last_success": time.time()}
    context = _margin_context()

    normal = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status=reconciler,
        context=context,
    )
    repair = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100", "inventory_repair": True},
        reconciler_status=reconciler,
        context=context,
    )
    paired = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100", "paired_net_neutral": True},
        reconciler_status=reconciler,
        context=context,
    )

    assert normal["allowed"] is True
    assert normal["reason_code"] == "active"
    assert normal["shadow"]["metrics"]["account_margin_mode"] == "THROTTLED"
    assert normal["shadow"]["metrics"]["account_margin_soft_exceeded"] is True
    assert repair["allowed"] is True
    assert paired["allowed"] is True
    assert paired["shadow"]["metrics"]["account_margin_hard_limit"] == 0.55


def test_order_budget_reservation_is_counted_once_at_final_gate():
    context = _context()
    context["account"].update({
        "account_budget_enabled": True,
        "budget_remaining_margin": 0.0,
        "budget_order_reservation_margin": 55.0,
        "leverage": 2.0,
        "margin_buffer_ratio": 0.10,
    })
    decision = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=context,
    )

    assert decision["allowed"] is True
    assert decision["shadow"]["metrics"]["required_margin"] == pytest.approx(55.0)
    assert decision["shadow"]["metrics"]["budget_available_for_order_margin"] == pytest.approx(55.0)


def test_order_budget_cannot_claim_capacity_without_its_reservation():
    context = _context()
    context["account"].update({
        "account_budget_enabled": True,
        "budget_remaining_margin": 0.0,
        "leverage": 2.0,
        "margin_buffer_ratio": 0.10,
    })
    decision = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=context,
    )

    assert decision["allowed"] is False
    assert decision["reason_code"] == "account_margin_budget"


def test_stale_exchange_account_observation_blocks_opening():
    context = _context()
    context["account"]["risk_context_max_age_sec"] = 30.0
    context["account_risk"]["observed_at_ms"] = (time.time() - 120.0) * 1000.0
    decision = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=context,
    )

    assert decision["allowed"] is False
    assert decision["reason_code"] == "account_risk_stale"


def test_live_futures_guard_fails_closed_without_event_ledger():
    engine = MainEngine.__new__(MainEngine)
    engine.client = object()
    engine.config = {"global": {"enable_futures": True}}
    engine.event_ledger = None
    engine.command_bus = object()
    engine.risk_gate = None
    engine.reconciler = None

    with pytest.raises(RuntimeError, match="event ledger"):
        engine._require_live_futures_guard(["TEST-USDT-SWAP"])


def test_live_futures_guard_fails_closed_without_command_bus():
    engine = MainEngine.__new__(MainEngine)
    engine.client = object()
    engine.config = {"global": {"enable_futures": True}}
    engine.event_ledger = object()
    engine.command_bus = None
    engine.risk_gate = object()
    engine.reconciler = SimpleNamespace(get_status=lambda: {"ready": True})

    with pytest.raises(RuntimeError, match="command bus"):
        engine._require_live_futures_guard(["TEST-USDT-SWAP"])


def test_live_futures_guard_only_validates_ready_dependencies():
    engine = MainEngine.__new__(MainEngine)
    engine.client = object()
    engine.config = {"global": {"enable_futures": True}}
    engine.event_ledger = object()
    engine.command_bus = object()
    engine.risk_gate = object()
    engine.reconciler = SimpleNamespace(get_status=lambda: {"ready": True})
    engine._main_loop = lambda: pytest.fail("startup guard must not enter the main loop")

    engine._require_live_futures_guard(["TEST-USDT-SWAP"])


def test_main_loop_starts_after_strategy_initialization():
    source = inspect.getsource(MainEngine.start)
    assert source.index("self._init_strategies()") < source.index("self._main_loop()")


def test_multi_profile_config_keeps_isolated_data_dir():
    engine = MainEngine.__new__(MainEngine)
    engine._data_dir = "/tmp/okx-paper-isolation"
    config = {
        "futures": {"symbol": "DRAM-USDT-SWAP"},
        "profile": {},
    }

    built = engine._build_strategy_config(config, False)

    assert built["user_data_dir"] == "/tmp/okx-paper-isolation"


def test_hard_margin_latch_uses_hard_and_resume_lines():
    RiskPolicy._hard_margin_latches.clear()
    reconciler = {"ready": True, "last_success": time.time()}

    hard = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status=reconciler,
        context=_margin_context(0.56),
    )
    recovered = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status=reconciler,
        context=_margin_context(0.47),
    )

    assert hard["reason_code"] == "account_hard_margin_limit"
    assert recovered["reason_code"] == "active"
    assert recovered["shadow"]["metrics"]["account_margin_hard_latched"] is False


def test_margin_below_sixty_percent_releases_hard_latch():
    RiskPolicy._hard_margin_latches.clear()
    reconciler = {"ready": True, "last_success": time.time()}

    hard_context = _margin_context(0.60)
    hard_context["account"]["account_margin_hard_limit"] = 0.60
    hard_context["account"]["account_margin_resume_limit"] = 0.599
    hard = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status=reconciler,
        context=hard_context,
    )

    recovered_context = _margin_context(0.562)
    recovered_context["account"]["account_margin_hard_limit"] = 0.60
    recovered_context["account"]["account_margin_resume_limit"] = 0.599
    recovered = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status=reconciler,
        context=recovered_context,
    )

    assert hard["reason_code"] == "account_hard_margin_limit"
    assert recovered["reason_code"] == "active"
    assert recovered["shadow"]["metrics"]["account_margin_hard_latched"] is False


def test_opening_without_symbol_context_fails_closed():
    RiskPolicy._hard_margin_latches.clear()
    decision = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "1", "px": "100"},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=None,
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "risk_context_unavailable"


def test_conflicting_intent_markers_fail_closed():
    decision = RiskPolicy.evaluate_order(
        "ACTIVE", "TEST-USDT-SWAP", "buy", "long", False,
        payload={
            "quantity": "1",
            "px": "100",
            "inventory_repair": True,
            "paired_net_neutral": True,
        },
        reconciler_status={"ready": True, "last_success": time.time()},
        context=_context(long_qty=10.0, short_qty=8.0),
    )
    assert decision["allowed"] is False
    assert decision["reason_code"] == "order_intent_invalid"

def test_fart_hard_margin_override_allows_only_net_reducing_long_repair():
    RiskPolicy._hard_margin_latches.clear()
    context = _crv_hard_override_context()
    context["symbol"] = "FARTCOIN-USDT-SWAP"
    context["account"].update({
        "account_margin_scope": "fart_override_test",
        "inventory_repair_hard_override_symbol": "FARTCOIN-USDT-SWAP",
        "leverage": 10.0,
        "mark_price": 0.1430,
        "inventory_repair_hard_override_max_step_notional_usdt": 8.0,
    })
    context["positions"].update({"long_qty": 0.0, "short_qty": 6543.0})
    allowed = RiskPolicy.evaluate_order(
        "ACTIVE", "FARTCOIN-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "55", "px": "0.1430", "inventory_repair": True},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=context,
    )
    assert allowed["reason_code"] == "inventory_repair_hard_margin_override_allowed"

    RiskPolicy._hard_margin_latches.clear()
    ordinary = RiskPolicy.evaluate_order(
        "ACTIVE", "FARTCOIN-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "55", "px": "0.1430"},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=context,
    )
    assert ordinary["reason_code"] == "account_hard_margin_limit"

def test_dram_hard_margin_override_allows_only_net_reducing_long_repair():
    RiskPolicy._hard_margin_latches.clear()
    context = _crv_hard_override_context()
    context["symbol"] = "DRAM-USDT-SWAP"
    context["account"].update({
        "account_margin_scope": "dram_override_test",
        "inventory_repair_hard_override_symbol": "DRAM-USDT-SWAP",
        "leverage": 3.0,
        "mark_price": 59.42,
        "inventory_repair_hard_override_margin_ratio": 0.65,
        "inventory_repair_hard_override_max_step_notional_usdt": 8.0,
    })
    context["positions"].update({"long_qty": 0.0, "short_qty": 31.7})
    allowed = RiskPolicy.evaluate_order(
        "ACTIVE", "DRAM-USDT-SWAP", "buy", "long", False,
        payload={"quantity": "0.1", "px": "59.42", "inventory_repair": True},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=context,
    )
    assert allowed["reason_code"] == "inventory_repair_hard_margin_override_allowed"
