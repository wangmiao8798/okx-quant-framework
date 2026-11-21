from strategies.futures_hedge import state_storage_path


def test_default_state_storage_stays_in_project_data():
    assert state_storage_path({}, 'fee_pnl.json') == 'data/fee_pnl.json'


def test_customer_state_storage_uses_tenant_directory(tmp_path):
    assert state_storage_path({'user_data_dir': str(tmp_path)}, 'fee_pnl.json') == str(tmp_path / 'fee_pnl.json')
