from pathlib import Path
from types import SimpleNamespace

from execution.gateway import ExecutionGateway


class FakeClient:
    def __init__(self):
        self.calls = []
        self._trade = SimpleNamespace(cancel_order=object())

    def place_order(self, symbol, side, quantity, **kwargs):
        self.calls.append(("place", symbol, side, quantity, kwargs))
        return {"ordId": "order-1"}

    def _call(self, method, **kwargs):
        assert method is self._trade.cancel_order
        self.calls.append(("cancel", kwargs))
        return {"code": "0", "data": [{"sCode": "0"}]}

    def set_leverage(self, symbol, leverage, margin_mode):
        self.calls.append(("leverage", symbol, leverage, margin_mode))
        return {"code": "0"}

    def close_all(self, symbol, margin_mode):
        self.calls.append(("close", symbol, margin_mode))


def test_execution_gateway_delegates_all_exchange_writes():
    client = FakeClient()
    gateway = ExecutionGateway(client)

    placed = gateway.place_order("TEST-USDT-SWAP", "buy", 1, pos_side="long")
    cancelled = gateway.cancel_order("TEST-USDT-SWAP", "order-1")
    gateway.set_leverage("TEST-USDT-SWAP", 2, "cross")
    gateway.close_all("TEST-USDT-SWAP")

    assert placed["ordId"] == "order-1"
    assert cancelled is True
    assert client.calls == [
        ("place", "TEST-USDT-SWAP", "buy", "1", {"pos_side": "long"}),
        ("cancel", {"instId": "TEST-USDT-SWAP", "ordId": "order-1"}),
        ("leverage", "TEST-USDT-SWAP", 2, "cross"),
        ("close", "TEST-USDT-SWAP", "cross"),
    ]


def test_exchange_writes_exist_only_in_client_and_gateway():
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "self._client.place_order(",
        "client.place_order(",
        "self._client.set_leverage(",
        "self._client.close_all(",
        "._trade.cancel_order",
    )
    allowed = {
        root / "api" / "okx_client.py",
        root / "execution" / "gateway.py",
    }
    offenders = []
    runtime_paths = []
    for directory in ("api", "core", "execution", "risk", "strategies", "utils", "web", "scripts"):
        runtime_paths.extend((root / directory).rglob("*.py"))
    runtime_paths.extend(path for path in root.glob("*.py") if path.is_file())
    for path in runtime_paths:
        if path in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        for marker in forbidden:
            if marker in source:
                offenders.append((str(path.relative_to(root)), marker))
    assert offenders == []
