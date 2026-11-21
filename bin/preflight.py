#!/opt/okx_quant/venv/bin/python3
"""Deployment-time checks for the live strategy and execution boundary."""

from __future__ import annotations

import glob
import inspect
import pathlib
import py_compile
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml

from core.config_validation import validate_config_bundle
from risk.position_limits import opening_limits_block_reason
from strategies.futures_hedge import FuturesHedgeStrategy


def _check_capacity_contract() -> None:
    # A normal order must not inherit the repair side-cap exception.
    normal = opening_limits_block_reason(
        "short", 100.0, 100.0, 10.0, 1.0, 1.0, 105.0, 1000.0,
        inventory_repair_side_cap_multiplier=1.10,
        inventory_repair=False,
    )
    if normal != "max_side_notional":
        raise RuntimeError(f"normal capacity contract changed: {normal!r}")

    # An explicit net-reducing repair may use only its configured bounded room.
    repair = opening_limits_block_reason(
        "short", 100.0, 90.0, 10.0, 1.0, 1.0, 95.0, 1000.0,
        inventory_repair_side_cap_multiplier=1.10,
        inventory_repair=True,
    )
    if repair:
        raise RuntimeError(f"repair capacity contract changed: {repair!r}")

    signature = inspect.signature(FuturesHedgeStrategy._opening_order_block_reason)
    if "inventory_repair" not in signature.parameters:
        raise RuntimeError("opening guard has no explicit inventory_repair intent")


def main() -> int:
    config_path = ROOT / "config" / "config.yaml"
    try:
        with config_path.open(encoding="utf-8") as handle:
            main_config = yaml.safe_load(handle) or {}
        extras = []
        for path in sorted(glob.glob(str(ROOT / "config" / "config_*.yaml"))):
            with open(path, encoding="utf-8") as handle:
                extras.append((path, yaml.safe_load(handle) or {}))
        validation = validate_config_bundle(main_config, str(config_path), extras)
        if not validation.get("ok"):
            raise RuntimeError("; ".join(validation.get("errors") or ["config invalid"]))
        for relative in (
            "core/config_validation.py",
            "risk/position_limits.py",
            "risk/policy.py",
            "execution/gateway.py",
            "strategies/futures_hedge.py",
        ):
            py_compile.compile(str(ROOT / relative), doraise=True)
        _check_capacity_contract()
    except Exception as exc:
        print(f"[preflight] FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"[preflight] OK profiles={len(validation.get('profiles') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
