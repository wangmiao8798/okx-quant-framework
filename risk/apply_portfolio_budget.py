#!/usr/bin/env python3
"""Conservative portfolio budget applier.

Default is dry-run. It only changes config after consecutive matching budget
signals. It never expands a symbol under freeze/high-pressure action.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

STATE = Path("data/portfolio_budget_apply_state.json")
PLAN = Path("data/portfolio_budget_plan.json")
AUDIT_DIR = Path("reports/portfolio_budget")
STATUS = Path("data/strategies_status.json")
STATUS_MAX_AGE_SEC = 180.0
CONFIG_BY_SYMBOL = {
    "CRV-USDT-SWAP": Path("config/config.yaml"),
    "DRAM-USDT-SWAP": Path("config/config_dram.yaml"),
    "FARTCOIN-USDT-SWAP": Path("config/config_fartcoin.yaml"),
}


def run_plan() -> dict:
    subprocess.run([sys.executable, "risk/portfolio_budget.py"], check=True)
    return json.loads(PLAN.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"symbols": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _budget_gate(status: dict, symbol: str, now: float | None = None) -> tuple[bool, str]:
    now = float(now if now is not None else time.time())
    if not status:
        return False, "status_missing"
    updated = float(status.get("updated_at") or 0)
    if updated <= 0 or now - updated > STATUS_MAX_AGE_SEC:
        return False, "status_stale"
    gate = status.get("risk_gate") or {}
    if str(gate.get("state") or "").upper() != "ACTIVE":
        return False, "risk_gate_not_active"
    strategy = next((v for v in (status.get("strategies") or {}).values() if (v.get("symbol") or "") == symbol), None)
    if not strategy:
        return False, "strategy_missing"
    if bool(strategy.get("halted")) or bool(strategy.get("resume_required")):
        return False, "strategy_halted"
    equity = float(strategy.get("cached_equity") or 0)
    if equity <= 0:
        return False, "equity_invalid"
    return True, ""


def proposed_patch(symbol: str, action: str, cfg: dict, consecutive: int, last_apply_ts: int = 0, cooldown_hours: float = 6.0, *, gate_ok: bool = False) -> dict:
    if not gate_ok:
        return {}
    futures = cfg.get("futures") or {}
    margin = float(futures.get("max_margin_ratio") or 0.0)
    grid = float(futures.get("grid_expand_margin_ratio") or margin)
    if last_apply_ts and time.time() - last_apply_ts < cooldown_hours * 3600:
        return {}
    if action == "increase_soft_budget_small" and consecutive >= 2:
        return {
            "max_margin_ratio": round(clamp(margin + 0.03, 0.05, 0.65), 4),
            "grid_expand_margin_ratio": round(clamp(max(grid, margin) + 0.03, 0.08, 0.70), 4),
        }
    if action == "freeze_new_budget":
        return {}
    return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write config changes if eligible")
    ap.add_argument("--restart", action="store_true", help="restart okx-quant.service after applying")
    ap.add_argument("--cooldown-hours", type=float, default=6.0, help="minimum hours between real config patches per symbol")
    args = ap.parse_args()

    plan = run_plan()
    state = load_state()
    live_status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}
    now = int(time.time())
    changes = []
    decisions = []
    for row in plan.get("symbols", []):
        symbol = row.get("symbol")
        action = row.get("action")
        st = state.setdefault("symbols", {}).setdefault(symbol, {"last_action": "", "consecutive": 0})
        if st.get("last_action") == action:
            st["consecutive"] = int(st.get("consecutive") or 0) + 1
        else:
            st["last_action"] = action
            st["consecutive"] = 1
        st["updated_at"] = now
        path = CONFIG_BY_SYMBOL.get(symbol)
        patch = {}
        gate_ok, gate_reason = _budget_gate(live_status, symbol)
        if path and path.exists():
            cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            patch = proposed_patch(symbol, action, cfg, st["consecutive"], int(st.get("last_apply_ts") or 0), args.cooldown_hours, gate_ok=gate_ok)
            if action == "increase_soft_budget_small" and not gate_ok:
                action = f"blocked_{gate_reason}"
            if patch:
                before = {k: (cfg.get("futures") or {}).get(k) for k in patch}
                changes.append({"symbol": symbol, "path": str(path), "before": before, "after": patch})
                if args.apply:
                    backup = Path("/tmp") / f"{path.name}.portfolio_budget.{time.strftime('%Y%m%d_%H%M%S')}"
                    shutil.copy2(path, backup)
                    fut = cfg.setdefault("futures", {})
                    fut.update(patch)
                    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
                    st["last_apply_ts"] = now
                    st["last_backup"] = str(backup)
        decisions.append({
            "symbol": symbol,
            "action": action,
            "consecutive": st["consecutive"],
            "eligible_patch": patch,
            "score": row.get("score"),
            "reasons": row.get("reasons", []),
            "gate_reason": gate_reason,
        })
    save_state(state)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    audit = {"ts": now, "apply": args.apply, "changes": changes, "decisions": decisions}
    audit_path = AUDIT_DIR / f"portfolio_budget_apply_{now}.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"audit={audit_path}")
    print("Portfolio budget apply", "APPLY" if args.apply else "DRY-RUN")
    for d in decisions:
        print("{symbol} action={action} consecutive={consecutive} patch={eligible_patch} score={score} reasons={reasons}".format(
            **{**d, "reasons": ",".join(d.get("reasons") or [])}
        ))
    if args.apply and changes and args.restart:
        subprocess.run(["sudo", "systemctl", "restart", "okx-quant.service"], check=True)
        time.sleep(12)
        subprocess.run(["systemctl", "is-active", "okx-quant.service"], check=True)


if __name__ == "__main__":
    main()
