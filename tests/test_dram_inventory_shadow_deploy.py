from pathlib import Path

ROOT = Path('/opt/okx_quant')


def test_shadow_service_is_isolated_from_trading_engine():
    service = (ROOT / 'deploy/systemd/okx-dram-inventory-shadow.service').read_text(encoding='utf-8')
    timer = (ROOT / 'deploy/systemd/okx-dram-inventory-shadow.timer').read_text(encoding='utf-8')
    assert 'scripts/dram_inventory_shadow.py' in service
    assert 'ReadWritePaths=/opt/okx_quant/data' in service
    assert 'NoNewPrivileges=true' in service
    assert 'ProtectSystem=strict' in service
    assert 'okx-quant.service' not in service
    assert 'OnUnitActiveSec=1min' in timer
