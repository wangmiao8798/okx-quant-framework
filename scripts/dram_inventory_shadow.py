#!/usr/bin/env python3
"""Read-only DRAM inventory repair shadow. No trading mutations are invoked."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inventory.hedge_inventory import HedgeInventory, Pos

DEFAULT_DB = ROOT / "data/dram_inventory_shadow.db"
DEFAULT_REPORT = ROOT / "data/dram_inventory_shadow_report.json"
STATUS = ROOT / "data/strategies_status.json"
CONFIG = ROOT / "config/config_dram.yaml"
HORIZONS = (5, 30, 60, 240)


def decide_candidate(
    state: dict[str, Any],
    *,
    pending_repair_qty: float = 0.0,
    imbalance_gate: float = 0.70,
    stage_ratio: float = 0.30,
) -> dict[str, Any]:
    long_qty = float(state.get("long_qty") or 0)
    short_qty = float(state.get("short_qty") or 0)
    inventory = HedgeInventory()
    inventory.long = Pos("long", qty=long_qty)
    inventory.short = Pos("short", qty=short_qty)
    plan = inventory.repair_plan(
        float(state.get("mark_price") or 0),
        float(state.get("contract_value") or 1.0),
        float(state.get("max_net_notional") or 0),
        imbalance_gate,
        stage_ratio,
    )
    pending_repair_qty = max(0.0, float(pending_repair_qty or 0.0))
    uncovered_qty = max(0.0, float(plan["proposed_qty"]) - pending_repair_qty)
    return {
        "candidate_action": f"repair_{plan['repair_side']}" if plan["active"] else "as_is",
        "repair_active": bool(plan["active"]),
        "repair_side": plan["repair_side"],
        "dominant_side": plan["dominant_side"],
        "proposed_qty": plan["proposed_qty"],
        "pending_repair_qty": pending_repair_qty,
        "uncovered_repair_qty": uncovered_qty,
        "net_notional": plan["net_notional"],
        "imbalance_ratio": plan["imbalance_ratio"],
        "suppress_open_long": False,
        "suppress_open_short": False,
        "advisory_only": True,
        "gates": {"imbalance": imbalance_gate, "stage_ratio": stage_ratio},
    }


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots(
      ts INTEGER PRIMARY KEY, mark_price REAL, equity REAL, margin_load_ratio REAL,
      long_qty REAL, short_qty REAL, imbalance_ratio REAL, candidate_action TEXT,
      open_long_orders INTEGER, open_short_orders INTEGER, payload_json TEXT);
    CREATE TABLE IF NOT EXISTS candidate_intents(
      order_id TEXT PRIMARY KEY, first_seen_ts INTEGER, order_price REAL, order_qty REAL,
      fill_ts INTEGER, fill_price REAL, fill_qty REAL, fill_fee REAL DEFAULT 0,
      status TEXT NOT NULL, payload_json TEXT);
    CREATE TABLE IF NOT EXISTS mark_outcomes(
      order_id TEXT, horizon_minutes INTEGER, outcome_ts INTEGER, mark_price REAL,
      baseline_long_pnl REAL, avoided_pnl REAL, payload_json TEXT,
      PRIMARY KEY(order_id,horizon_minutes));
    """)


def _state() -> dict[str, Any]:
    doc = json.loads(STATUS.read_text(encoding="utf-8"))
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    row = (doc.get("strategies") or {}).get("futures_dram") or {}
    monitor = row.get("monitor") or {}
    return {
        "long_qty": float(row.get("long_qty") or 0),
        "short_qty": float(row.get("short_qty") or 0),
        "mark_price": float(monitor.get("price") or 0),
        "equity": float(monitor.get("cached_equity") or row.get("cached_equity") or 0),
        "margin_load_ratio": float(monitor.get("account_margin_load_ratio") or monitor.get("margin_load_ratio") or 0),
        "imbalance_ratio": float(monitor.get("imbalance_ratio") or 0),
        "contract_value": 1.0,
        "max_net_notional": float(config.get("futures", {}).get("max_net_notional_usdt") or 0),
        "updated_at": doc.get("updated_at"),
    }


def _client():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "web"))
    from api.crypto_utils import CredentialManager
    from api.okx_client import OKXClient
    from config_manager import load_api_password_secure
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    cm = CredentialManager(cfg)
    cm.set_password(load_api_password_secure())
    creds = cm.load()
    flag = str(cfg.get("exchange", {}).get("okx", {}).get("flag", "0"))
    return OKXClient(creds, flag=flag)


def _read_exchange(client) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    orders_resp = client._call(client._trade.get_order_list, instId="DRAM-USDT-SWAP", state="live")
    fills_resp = client._call(client._trade.get_fills, instType="SWAP", instId="DRAM-USDT-SWAP", limit="100")
    for label, resp in (("orders", orders_resp), ("fills", fills_resp)):
        if str(resp.get("code", "")) != "0":
            raise RuntimeError(f"{label} query failed: code={resp.get('code')} msg={resp.get('msg','')}")
    return orders_resp.get("data") or [], fills_resp.get("data") or []


def _is_open(order: dict[str, Any], pos_side: str) -> bool:
    reduce_only = str(order.get("reduceOnly", "false")).lower() == "true"
    expected_side = "buy" if pos_side == "long" else "sell"
    return not reduce_only and order.get("posSide") == pos_side and order.get("side") == expected_side


def record_sample(db: Path, state: dict[str, Any], orders: list[dict[str, Any]], fills: list[dict[str, Any]], now_ms: int | None = None) -> dict[str, Any]:
    now_ms = int(now_ms or time.time() * 1000)
    open_longs = [o for o in orders if _is_open(o, "long")]
    open_shorts = [o for o in orders if _is_open(o, "short")]
    initial = decide_candidate(state)
    repair_orders = open_longs if initial["repair_side"] == "long" else open_shorts
    pending_repair_qty = sum(abs(float(order.get("sz") or 0)) for order in repair_orders)
    decision = decide_candidate(state, pending_repair_qty=pending_repair_qty)
    mark = float(state.get("mark_price") or 0)
    with sqlite3.connect(db, timeout=30) as conn:
        _ensure_schema(conn)
        payload = {"state": state, "decision": decision, "advisory_only": True}
        conn.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            now_ms, mark, float(state.get("equity") or 0), float(state.get("margin_load_ratio") or 0),
            float(state.get("long_qty") or 0), float(state.get("short_qty") or 0), float(state.get("imbalance_ratio") or 0),
            decision["candidate_action"], len(open_longs), len(open_shorts), json.dumps(payload, sort_keys=True)))
        if decision["suppress_open_long"]:
            for order in open_longs:
                oid = str(order.get("ordId") or "")
                if oid:
                    row = {"order_id": oid, "first_seen_ts": now_ms, "order_price": float(order.get("px") or 0), "order_qty": float(order.get("sz") or 0), "advisory_only": True}
                    conn.execute("INSERT OR IGNORE INTO candidate_intents(order_id,first_seen_ts,order_price,order_qty,status,payload_json) VALUES(?,?,?,?,?,?)",
                                 (oid, now_ms, row["order_price"], row["order_qty"], "observed", json.dumps(row, sort_keys=True)))
        intents = {r[0] for r in conn.execute("SELECT order_id FROM candidate_intents WHERE fill_ts IS NULL")}
        for fill in fills:
            oid = str(fill.get("ordId") or "")
            if oid in intents and fill.get("side") == "buy" and fill.get("posSide") == "long":
                conn.execute("UPDATE candidate_intents SET fill_ts=?,fill_price=?,fill_qty=?,fill_fee=?,status='filled' WHERE order_id=?",
                             (int(fill.get("ts") or now_ms), float(fill.get("fillPx") or 0), float(fill.get("fillSz") or 0), abs(float(fill.get("fee") or 0)), oid))
        filled = conn.execute("SELECT order_id,fill_ts,fill_price,fill_qty,fill_fee FROM candidate_intents WHERE fill_ts IS NOT NULL").fetchall()
        for oid, fill_ts, fill_px, fill_qty, fill_fee in filled:
            for horizon in HORIZONS:
                if now_ms >= int(fill_ts) + horizon * 60_000:
                    exists = conn.execute("SELECT 1 FROM mark_outcomes WHERE order_id=? AND horizon_minutes=?", (oid, horizon)).fetchone()
                    if not exists:
                        baseline_pnl = (mark - float(fill_px)) * float(fill_qty) - float(fill_fee)
                        avoided = -baseline_pnl
                        out = {"order_id": oid, "horizon_minutes": horizon, "fill_price": fill_px, "mark_price": mark, "baseline_long_pnl": baseline_pnl, "avoided_pnl": avoided, "advisory_only": True}
                        conn.execute("INSERT INTO mark_outcomes VALUES(?,?,?,?,?,?,?)", (oid, horizon, now_ms, mark, baseline_pnl, avoided, json.dumps(out, sort_keys=True)))
        conn.commit()
    return {"ts": now_ms, "decision": decision, "open_long_orders": len(open_longs), "open_short_orders": len(open_shorts), "advisory_only": True}


def build_report(db: Path) -> dict[str, Any]:
    with sqlite3.connect(db, timeout=30) as conn:
        _ensure_schema(conn)
        snapshots = conn.execute("SELECT count(*) FROM snapshots").fetchone()[0]
        intents = conn.execute("SELECT count(*) FROM candidate_intents").fetchone()[0]
        filled = conn.execute("SELECT count(*) FROM candidate_intents WHERE fill_ts IS NOT NULL").fetchone()[0]
        rows = conn.execute("SELECT horizon_minutes,count(*),avg(avoided_pnl),sum(avoided_pnl) FROM mark_outcomes GROUP BY horizon_minutes ORDER BY horizon_minutes").fetchall()
        latest = conn.execute("SELECT ts,candidate_action,long_qty,short_qty,margin_load_ratio,mark_price,payload_json FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    latest_payload = json.loads(latest[6]) if latest else {}
    return {
        "generated_at": int(time.time()), "advisory_only": True, "readonly": True,
        "policy": "stage a 30% weak-side repair when imbalance>=70% or net notional exceeds the configured cap",
        "snapshots": snapshots, "candidate_intents": 0, "candidate_intents_filled": 0,
        "outcomes": {},
        "legacy_suppression_experiment": {
            "candidate_intents": intents,
            "candidate_intents_filled": filled,
            "outcomes": {str(h): {"samples": n, "avg_avoided_pnl_usdt": avg, "sum_avoided_pnl_usdt": total} for h, n, avg, total in rows},
        },
        "latest": {
            **dict(zip(("ts", "candidate_action", "long_qty", "short_qty", "margin_load_ratio", "mark_price"), latest[:6])),
            "decision": latest_payload.get("decision", {}),
        } if latest else None,
        "interpretation": "uncovered_repair_qty is the staged repair not already covered by passive opening orders",
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = p.parse_args()
    state = _state()
    orders, fills = _read_exchange(_client())
    sample = record_sample(args.db, state, orders, fills)
    report = build_report(args.db)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"sample": sample, "report": report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
