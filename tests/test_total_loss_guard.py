import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from risk.total_loss_guard import TotalLossGuard


def _guard(monkeypatch, on_stop=None):
    clock = [1000.0]
    monkeypatch.setattr(
        "risk.total_loss_guard.time.time", lambda: clock[0]
    )
    config = {
        "risk": {
            "stop_loss": {
                "enabled": True,
                "total_loss_pct": 4,
                "cooldown_seconds": 600,
                "account_data_grace_seconds": 30,
            }
        }
    }
    guard = TotalLossGuard(config, on_stop=on_stop)
    guard.set_initial_equity(1000.0)
    return guard, clock


def test_invalid_equity_within_grace_does_not_halt(monkeypatch):
    calls = []
    guard, clock = _guard(monkeypatch, on_stop=lambda: calls.append("stop"))
    assert guard.check(0.0) is True
    assert guard.halted is False
    assert calls == []
    clock[0] += 10
    assert guard.check(0.0) is True
    assert guard.halted is False


def test_persistent_invalid_equity_halts_without_emergency_close(monkeypatch):
    calls = []
    guard, clock = _guard(monkeypatch, on_stop=lambda: calls.append("stop"))
    assert guard.check(0.0) is True
    clock[0] += 31
    assert guard.check(0.0) is False
    assert guard.halted is True
    assert guard.halt_reason == "account_data_unavailable"
    assert calls == []


def test_valid_equity_recovers_from_data_unavailable(monkeypatch):
    guard, clock = _guard(monkeypatch)
    guard.check(0.0)
    clock[0] += 31
    assert guard.check(0.0) is False
    assert guard.check(980.0) is True
    assert guard.halted is False
    assert guard.halt_reason == ""


def test_real_total_loss_still_triggers_emergency_callback(monkeypatch):
    calls = []
    guard, _clock = _guard(monkeypatch, on_stop=lambda: calls.append("stop"))
    assert guard.check(959.0) is False
    assert guard.halted is True
    assert guard.halt_reason == "global_total_loss"
    assert calls == ["stop"]
