import json
from scripts.send_daily_profit_report import refresh_harvest_report


def test_refresh_harvest_report_writes_latest_payload(tmp_path):
    output = tmp_path / 'harvest.json'
    payload = {'window_hours': 72, 'symbols': {'CRV-USDT-SWAP': {}}}
    assert refresh_harvest_report(builder=lambda hours: payload, output=output) == payload
    assert json.loads(output.read_text(encoding='utf-8')) == payload
