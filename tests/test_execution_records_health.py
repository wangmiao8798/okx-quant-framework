import os
import tempfile
import time
from pathlib import Path

from web import routes


def test_execution_records_uses_the_newest_strategy_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data = root / "data"
        data.mkdir()
        now = time.time()
        paths = [
            ("data/pnl_records.csv", "execution_records:CRV", now - 130_000),
            ("data/pnl_records_dram.csv", "execution_records:DRAM", now - 53_000),
            ("data/pnl_records_fartcoin.csv", "execution_records:FARTCOIN", now - 20_000),
        ]
        for rel_path, _label, modified_at in paths:
            path = root / rel_path
            path.write_text("time,symbol\n", encoding="utf-8")
            os.utime(path, (modified_at, modified_at))

        previous_project = routes.PROJ
        routes.PROJ = root
        try:
            aggregate, members = routes._latest_file_health(
                [(rel_path, label) for rel_path, label, _modified_at in paths],
                warn_after=86_400,
                critical_after=604_800,
                allow_empty=True,
                label="execution_records",
            )
        finally:
            routes.PROJ = previous_project

    assert aggregate["status"] == "ok"
    assert aggregate["name"] == "execution_records"
    assert aggregate["note"].endswith("pnl_records_fartcoin.csv")
    assert [member["status"] for member in members] == ["warn", "ok", "ok"]
