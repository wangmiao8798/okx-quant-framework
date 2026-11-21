#!/usr/bin/env python3
"""Read-only OKX reduce-only TP coverage audit."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "data" / "strategies_status.json"
OUT_PATH = ROOT / "data" / "tp_coverage_audit.json"


def _float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _is_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def calculate_coverage(orders: list[dict], long_qty: float, short_qty: float, long_entry: float = 0.0, short_entry: float = 0.0) -> dict:
    totals = {"long": 0.0, "short": 0.0}
    counts = {"long": 0, "short": 0}
    excluded_non_tp = {"long": 0, "short": 0}
    entries = {"long": _float(long_entry), "short": _float(short_entry)}
    seen = set()
    for order in orders or []:
        state = str(order.get("state") or "live").lower()
        if state not in {"live", "partially_filled", "partially-filled", "partiallyfilled"}:
            continue
        if not _is_true(order.get("reduceOnly")):
            continue
        pos_side = str(order.get("posSide") or "").lower()
        side = str(order.get("side") or "").lower()
        if (pos_side, side) not in {("long", "sell"), ("short", "buy")}:
            continue
        order_id = str(order.get("ordId") or "").strip()
        if not order_id:
            # Exchange live orders should always have ordId. Do not guess/dedupe
            # anonymous orders because two identical orders may both be legitimate.
            continue
        if order_id in seen:
            continue
        seen.add(order_id)
        px = _float(order.get("px"))
        entry = entries[pos_side]
        if entry > 0 and ((pos_side == "long" and px <= entry) or (pos_side == "short" and px >= entry)):
            excluded_non_tp[pos_side] += 1
            continue
        remaining = max(0.0, _float(order.get("sz")) - _float(order.get("accFillSz")))
        if remaining <= 0:
            continue
        totals[pos_side] += remaining
        counts[pos_side] += 1

    result = {}
    for pos_side, position_qty in (("long", _float(long_qty)), ("short", _float(short_qty))):
        tp_qty = round(totals[pos_side], 8)
        if position_qty <= 0:
            result[pos_side] = {
                "position_qty": 0.0,
                "tp_remaining_qty": tp_qty,
                "coverage_pct": None,
                "missing_qty": 0.0,
                "excess_qty": tp_qty,
                "order_count": counts[pos_side],
                "excluded_non_tp_count": excluded_non_tp[pos_side],
                "status": "not_applicable",
            }
            continue
        raw_pct = tp_qty / position_qty * 100
        missing = max(0.0, position_qty - tp_qty)
        excess = max(0.0, tp_qty - position_qty)
        result[pos_side] = {
            "position_qty": round(position_qty, 8),
            "tp_remaining_qty": tp_qty,
            "coverage_pct": round(min(100.0, raw_pct), 4),
            "raw_coverage_pct": round(raw_pct, 4),
            "missing_qty": round(missing, 8),
            "excess_qty": round(excess, 8),
            "order_count": counts[pos_side],
            "excluded_non_tp_count": excluded_non_tp[pos_side],
            "status": "covered" if missing <= 1e-8 else ("partial" if tp_qty > 0 else "missing"),
        }
    return result


def _load_status() -> dict:
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def _make_client(config_path: Path):
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "web"))
    import yaml
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    from config_manager import load_api_password_secure

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cm = CredentialManager(cfg)
    cm.set_password(load_api_password_secure())
    creds = cm.load()
    flag = str(cfg.get("exchange", {}).get("okx", {}).get("flag", "0"))
    return OKXClient(creds, flag=flag)


def _config_for_runtime(runtime: str) -> Path:
    mapping = {
        "futures": ROOT / "config" / "config.yaml",
        "futures_dram": ROOT / "config" / "config_dram.yaml",
        "futures_fartcoin": ROOT / "config" / "config_fartcoin.yaml",
    }
    return mapping.get(runtime, ROOT / "config" / "config.yaml")


def _status_age_seconds() -> float:
    try:
        return max(0.0, time.time() - STATUS_PATH.stat().st_mtime)
    except OSError:
        return float("inf")


def audit_live() -> dict:
    status = _load_status()
    rows = {}
    clients = {}
    for runtime, state in (status.get("strategies") or {}).items():
        symbol = state.get("symbol") or runtime
        cfg_path = _config_for_runtime(runtime)
        key = str(cfg_path)
        if key not in clients:
            clients[key] = _make_client(cfg_path)
        client = clients[key]
        # Use raw API call so an API failure cannot be silently converted to [].
        order_resp = client._call(client._trade.get_order_list, instId=symbol, state="live")
        if str(order_resp.get("code", "")) != "0":
            raise RuntimeError(f"order query failed for {symbol}: code={order_resp.get('code')} msg={order_resp.get('msg','')}")
        orders = order_resp.get("data", []) or []
        positions = client.get_positions(symbol) or []
        exchange_qty = {"long": 0.0, "short": 0.0}
        exchange_entry = {"long": 0.0, "short": 0.0}
        for pos in positions:
            pside = str(pos.get("posSide") or "").lower()
            if pside in exchange_qty:
                exchange_qty[pside] += _float(pos.get("pos"))
                exchange_entry[pside] = _float(pos.get("avgPx")) or exchange_entry[pside]
        coverage = calculate_coverage(
            orders,
            exchange_qty["long"], exchange_qty["short"],
            exchange_entry["long"], exchange_entry["short"],
        )
        rows[symbol] = {
            "runtime": runtime,
            "symbol": symbol,
            "coverage": coverage,
            "live_order_count": len(orders),
            "position_source": "okx_live_positions",
            "status_snapshot_age_sec": round(_status_age_seconds(), 2),
            "status": "stale" if _status_age_seconds() > 180 else "fresh",
            "readonly": True,
        }
    return {
        "generated_at": int(time.time()),
        "readonly": True,
        "source": "okx_live_orders",
        "symbols": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=OUT_PATH)
    args = parser.parse_args()
    payload = audit_live()
    args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TP coverage audit (read-only, exchange live orders)")
    for symbol, row in payload["symbols"].items():
        long = row["coverage"]["long"]
        short = row["coverage"]["short"]
        print(
            f"{symbol}: long={long['tp_remaining_qty']}/{long['position_qty']} "
            f"coverage={long['coverage_pct']}% status={long['status']}; "
            f"short={short['tp_remaining_qty']}/{short['position_qty']} "
            f"coverage={short['coverage_pct']}% status={short['status']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
