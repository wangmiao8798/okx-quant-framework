import ast
import sqlite3
from pathlib import Path

from control.command_bus import CommandBus
from core.engine import MainEngine


class LogStub:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class StrategyStub:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class RiskGateStub:
    def __init__(self):
        self.calls = []

    def set_state(self, state, reason, actor):
        self.calls.append((state, reason, actor))


def test_command_bus_is_idempotent_and_engine_consumes_commands(tmp_path):
    db_path = tmp_path / "trading.db"
    bus = CommandBus(str(db_path))
    fixed_id = "fixed-command"
    bus.enqueue("strategy_start", target="futures", command_id=fixed_id)
    bus.enqueue("strategy_start", target="other", command_id=fixed_id)
    bus.enqueue(
        "risk_state_set",
        payload={"state": "REDUCE_ONLY", "reason": "operator"},
        actor="web_admin",
    )
    strategy = StrategyStub()
    risk_gate = RiskGateStub()
    engine = MainEngine.__new__(MainEngine)
    engine.command_bus = bus
    engine.strategies = {"futures": strategy}
    engine.risk_gate = risk_gate
    engine.log = LogStub()

    engine._consume_control_commands()
    engine._consume_control_commands()

    with sqlite3.connect(db_path) as conn:
        commands = conn.execute(
            "SELECT command_type,target,status FROM control_commands ORDER BY created_ts,command_id"
        ).fetchall()
    bus.close()
    assert strategy.started == 1
    assert strategy.stopped == 0
    assert risk_gate.calls == [("REDUCE_ONLY", "operator", "web_admin")]
    assert set(commands) == {
        ("strategy_start", "futures", "COMPLETED"),
        ("risk_state_set", "", "COMPLETED"),
    }


def test_web_control_plane_uses_projections_and_command_outbox():
    root = Path(__file__).resolve().parents[1]
    source = (root / "web" / "routes.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "strategies_cmd.json" not in source
    assert "_enqueue_control_command(\"strategy_start\"" in functions["api_engine_start_strategy"]
    assert "_enqueue_control_command(\"strategy_stop\"" in functions["api_engine_stop_strategy"]
    assert "OKXClient" not in functions["get_account_status"]
    assert "CredentialManager" not in functions["get_account_status"]
    assert "OKXClient" not in functions["get_orders"]
    assert "_projected_positions()" in functions["get_account_status"]
    assert "_projected_live_orders()" in functions["get_orders"]

    service = (root / "systemd" / "okx-quant-web.service").read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in service
    assert "InaccessiblePaths=/opt/okx_quant/data/keys.enc /opt/okx_quant/bin" in service
    assert "ReadOnlyPaths=/opt/okx_quant/config" in service
