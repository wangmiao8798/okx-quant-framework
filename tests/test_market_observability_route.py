import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))

from web import routes


def test_market_observability_reports_fresh_closed_and_stale(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    now = 1_800_000_000

    (data / "strategies_status.json").write_text(json.dumps({
        "updated_at": now - 5,
        "strategies": {
            "futures": {
                "symbol": "DRAM-USDT-SWAP",
                "monitor": {
                    "moca_apply_live": False,
                    "moca_observed_state": "calm",
                    "moca_observed_confidence": 0.6,
                },
            },
        },
    }), encoding="utf-8")
    (data / "cross_market_opportunity_report.json").write_text(json.dumps({
        "generated_at": now - 60,
        "quotes": [{"symbol": "QQQ", "market_status": "Closed"}],
        "themes": [],
    }), encoding="utf-8")
    (data / "cross_market_forward_report.json").write_text("{}", encoding="utf-8")
    (data / "dram_cross_market_report.json").write_text(json.dumps({
        "generated_at_ms": (now - 1000) * 1000,
        "quotes": {"dram_etf": {"market_status": "Open"}},
        "signal": {"reasons": []},
    }), encoding="utf-8")
    (data / "dram_weighted_forward_report.json").write_text("{}", encoding="utf-8")
    (data / "dram_inventory_shadow_report.json").write_text(json.dumps({"readonly": True}), encoding="utf-8")

    monkeypatch.setattr(routes, "PROJ", tmp_path)
    result = routes._build_market_observability(now)

    assert result["moca"] == [{
        "symbol": "DRAM-USDT-SWAP",
        "state": "calm",
        "confidence": 0.6,
        "apply_live": False,
        "updated_at": now - 5,
        "age_sec": 5,
        "freshness": "fresh",
    }]
    assert result["radars"]["cross_market"]["freshness"] == "closed"
    assert result["radars"]["dram"]["freshness"] == "stale"
    assert result["radars"]["dram"]["inventory"] == {"readonly": True}
    assert result["radars"]["dram"]["inventory_freshness"] == "stale"
