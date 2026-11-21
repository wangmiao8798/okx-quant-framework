from web.vibe_research import build_vibe_research


def _write_journal(path, values, symbol="BTC-USDT-SWAP"):
    lines = ["time,symbol,side,qty,entry,exit,gross,fee,net"]
    for index, value in enumerate(values):
        lines.append(f"2026-07-{index + 1:02d} 00:00:00,{symbol},long,1,1,1,{value},0.01,{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_vibe_research_requires_out_of_sample_evidence(tmp_path):
    path = tmp_path / "pnl_records.csv"
    _write_journal(path, [1.0] * 9)
    report = build_vibe_research(path, "BTC-USDT-SWAP", 1000.0)
    assert report["advisory_only"] is True
    assert report["can_execute"] is False
    assert report["status"] == "collecting"
    assert report["training"]["trades"] == 6
    assert report["out_of_sample"]["trades"] == 3


def test_vibe_research_marks_positive_out_of_sample(tmp_path):
    path = tmp_path / "pnl_records.csv"
    training = [1.0] * 28
    test = [0.5] * 8 + [-0.25] * 4
    _write_journal(path, training + test)
    report = build_vibe_research(path, "BTC-USDT-SWAP", 1000.0)
    assert report["status"] == "positive_oos"
    assert report["out_of_sample"]["trades"] == 12
    assert report["out_of_sample"]["net_pnl"] == 3.0
    assert report["out_of_sample"]["profit_factor"] == 4.0


def test_vibe_research_isolates_symbol_and_detects_weak_oos(tmp_path):
    path = tmp_path / "pnl_records.csv"
    _write_journal(path, [1.0] * 28 + [-0.5] * 12)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("2026-08-01 00:00:00,ETH-USDT-SWAP,long,1,1,1,999,0,999\n")
    report = build_vibe_research(path, "BTC-USDT-SWAP", 1000.0)
    assert report["status"] == "weak_oos"
    assert report["out_of_sample"]["net_pnl"] == -6.0
    assert report["training"]["net_pnl"] == 28.0
