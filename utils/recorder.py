"""盈亏记录器：CSV交易记录、JSONL快照、汇总统计"""
"""
"""

import os, csv, json, shutil
from datetime import datetime
from typing import Dict


class PnLRecorder:
    def __init__(self, config: dict):
        rc = config.get("recorder", {})
        self._file = rc.get("pnl_file", "data/pnl_records.csv")
        self._export_dir = rc.get("export_dir", "data/exports")
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        os.makedirs(self._export_dir, exist_ok=True)

    def record_trade(self, trade: dict):
        exists = os.path.exists(self._file)
        with open(self._file, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=[
                "time","strategy","symbol","side",
                "price","quantity","cost","pnl","status","remark"
            ])
            if not exists:
                w.writeheader()
            trade["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w.writerow(trade)

    def record_snapshot(self, snap: dict):
        snap_file = "data/pnl_snapshots.jsonl"
        with open(snap_file, "a", encoding="utf-8") as f:
            snap["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")

    def get_summary(self) -> Dict:
        if not os.path.exists(self._file):
            return {"total_trades": 0, "total_pnl": 0.0, "win_rate": 0.0}
        total, wins, pnl = 0, 0, 0.0
        with open(self._file, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                total += 1
                p = float(row.get("pnl", 0))
                pnl += p
                if p > 0:
                    wins += 1
        return {
            "total_trades": total,
            "total_pnl": round(pnl, 4),
            "win_rate": round(wins/total*100, 2) if total > 0 else 0.0
        }

    def export_csv(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(self._export_dir, f"pnl_{ts}.csv")
        if os.path.exists(self._file):
            shutil.copy2(self._file, dst)
        return dst
