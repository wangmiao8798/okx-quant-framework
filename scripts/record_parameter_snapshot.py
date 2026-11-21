#!/usr/bin/env python3
"""Record current live parameter snapshot into config history.

Read-only for trading behavior: writes only data/config_history.jsonl so the
web panel can show parameter experiment versions.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

try:
    import yaml
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"pyyaml required: {exc}")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HISTORY = DATA / "config_history.jsonl"
FEEDBACK = DATA / "parameter_feedback_report.json"
CONFIGS = [ROOT / "config" / "config.yaml"] + sorted((ROOT / "config").glob("config_*.yaml"))
KEYS = [
    "symbol",
    "leverage",
    "base_position_usdt",
    "max_add_layers",
    "add_step",
    "first_tp_ratio",
    "final_tp_ratio",
    "max_margin_ratio",
    "reserved_usdt",
    "dynamic_quote_width_enabled",
    "dynamic_quote_width_min",
    "dynamic_quote_width_max",
    "hanging_tp_enabled",
    "startup_auto_hedge_enabled",
    "startup_rebuild_grid_enabled",
]


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def compact_config(path: Path) -> dict:
    cfg = load_yaml(path)
    fut = cfg.get("futures", {}) if isinstance(cfg, dict) else {}
    return {key: fut.get(key) for key in KEYS if key in fut}


def existing_duplicate(scope: str, after: dict) -> bool:
    if not HISTORY.exists():
        return False
    last = None
    with HISTORY.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if item.get("scope") == scope:
                last = item
    return bool(last and last.get("after") == after)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="parameter_snapshot")
    parser.add_argument("--note", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    feedback_rows = {row.get("symbol"): row for row in (load_json(FEEDBACK).get("symbols") or [])}
    DATA.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    written = 0
    with HISTORY.open("a", encoding="utf-8") as fh:
        for cfg_path in CONFIGS:
            if not cfg_path.exists():
                continue
            after = compact_config(cfg_path)
            if not after:
                continue
            symbol = after.get("symbol") or cfg_path.stem
            scope = f"parameter_snapshot:{cfg_path.relative_to(ROOT)}"
            payload = {
                "id": f"{now}_{cfg_path.stem}",
                "ts": now,
                "scope": scope,
                "source": args.source,
                "note": args.note,
                "change_count": len(after),
                "changed_keys": sorted(after.keys()),
                "before": {},
                "after": after,
                "feedback": feedback_rows.get(symbol, {}),
            }
            if not args.force and existing_duplicate(scope, after):
                print(f"skip_duplicate {cfg_path}")
                continue
            fh.write(json.dumps(payload, ensure_ascii=False) + chr(10))
            written += 1
            print(f"recorded {cfg_path} symbol={symbol}")
    print(f"written={written} history={HISTORY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
