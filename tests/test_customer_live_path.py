import sys
import tempfile
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "web"))

try:
    from utils.database import Database
except ModuleNotFoundError:
    from database import Database


def main():
    project_dir = PROJECT_DIR
    routes_path = project_dir / "web" / "routes.py"
    if not routes_path.exists():
        routes_path = project_dir / "routes.py"
    routes_source = routes_path.read_text(encoding="utf-8")
    assert routes_source.count('passphrase": passphrase}, flag="0")') == 1
    assert routes_source.count('passphrase": key_data["passphrase"]}, flag="0")') == 1
    assert '"ok": bool(funds_enough)' in routes_source
    assert 'strategy_config = _apply_customer_profile_defaults(strategy)' in routes_source
    assert 'len(password) < 8' in routes_source
    assert routes_source.index('key_data = db.get_user_api_key_data') < routes_source.index(
        'db.close()\n    if not key_data.get("ok")'
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(str(Path(tmp) / "test.db"))
        result = db.create_strategy(1, 1, "CRV-USDT-SWAP")
        assert result["ok"]
        strategy = db.get_user_strategies(1)[0]
        assert strategy["paper_mode"] is False
        assert strategy["max_daily_loss"] == 20
        assert strategy["max_total_loss"] == 50
        assert strategy["max_drawdown"] == 15
        db.close()

    from run_user_strategy import build_user_config, load_tested_profile

    profile_config = build_user_config({
        "symbol": "CRV-USDT-SWAP",
        "paper_mode": False,
        "max_daily_loss": 12,
        "max_total_loss": 30,
        "max_drawdown": 10,
    }, Path(tempfile.gettempdir()) / "customer-live-check")
    futures = profile_config["futures"]
    assert profile_config["exchange"]["okx"]["flag"] == "0"
    assert profile_config["global"]["enable_spot"] is False
    assert futures["symbol"] == "CRV-USDT-SWAP"
    # keep in sync with config/config.yaml instead of hardcoding the value
    import yaml as _yaml
    _crv = _yaml.safe_load((project_dir / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert futures["max_net_notional_usdt"] == _crv["futures"]["max_net_notional_usdt"]
    # hard_stop_enabled is operator-controlled (all three realised-loss breakers
    # were switched off after the 2026-10-23 flatten), so assert pass-through
    # rather than a fixed literal.
    assert futures["hard_stop_enabled"] == _crv["futures"]["hard_stop_enabled"]
    assert "rebalance_trigger_ratio" not in futures
    standby_config = build_user_config({
        "symbol": "BTC-USDT-SWAP",
        "paper_mode": False,
    }, Path(tempfile.gettempdir()) / "customer-standby-check")
    assert standby_config["futures"]["symbol"] == "BTC-USDT-SWAP"
    assert standby_config["futures"]["max_net_notional_usdt"] == 50
    assert standby_config["futures"]["hard_stop_enabled"] is False
    assert standby_config["exchange"]["okx"]["flag"] == "0"

    engine_path = project_dir / "core" / "engine.py"
    if not engine_path.exists():
        engine_path = project_dir / "engine.py"
    engine_source = engine_path.read_text(encoding="utf-8")
    assert 'self._data_dir = str(self.config.get("user_data_dir") or "data")' in engine_source
    assert 'Database(os.path.join(self._data_dir, "trading.db"))' in engine_source

    if (project_dir / "utils").is_dir():
        from strategies.futures_hedge import FuturesHedgeStrategy

        with tempfile.TemporaryDirectory() as tmp:
            isolated_config = build_user_config({
                "symbol": "CRV-USDT-SWAP",
                "paper_mode": False,
            }, Path(tmp))
            strategy = FuturesHedgeStrategy(isolated_config, None)
            assert Path(strategy._fee_pnl_file).parent == Path(tmp)
            assert Path(strategy._fee_pnl_file).exists()

    expected_symbols = {
        "CRV-USDT-SWAP",
        "FARTCOIN-USDT-SWAP",
        "BTC-USDT-SWAP",
        "BNB-USDT-SWAP",
        "ETH-USDT-SWAP",
        "SOL-USDT-SWAP",
        "XRP-USDT-SWAP",
        "DOGE-USDT-SWAP",
        "ETHFI-USDT-SWAP",
    }
    if (project_dir / "web").is_dir():
        sys.path.insert(0, str(project_dir / "web"))
        from web.routes import _customer_recommended_profiles

        profiles = _customer_recommended_profiles()
        symbols = {item["symbol"] for item in profiles}
        assert symbols == expected_symbols
        standby_profiles = [item for item in profiles if item["source"] == "standby"]
        # ETH 停用后，standby 归档里的同名 ETH 配置不再被 enabled 来源去重，会多计入 1 条
        eth_active = (project_dir / "config" / "config_eth.yaml").exists()
        assert len(standby_profiles) == (6 if eth_active else 7)
        assert all("备用" not in item["name"] and "待测" not in item["name"] for item in standby_profiles)
        assert all("待测" not in item["description"] and "尚未验证" not in item["description"] for item in standby_profiles)
        assert {item["risk_tag"] for item in standby_profiles} <= {"稳健", "波动", "实验"}
    else:
        assert all(f'"{symbol}"' in routes_source for symbol in expected_symbols)

    index_path = project_dir / "web" / "static" / "index.html"
    if not index_path.exists():
        index_path = project_dir / "index.html"
    index_source = index_path.read_text(encoding="utf-8")
    assert 'function openCustomerKeys()' in index_source
    assert 'if (!isAdminUser()) { REF = setInterval(customerLoad, 10000); return; }' in index_source
    assert '密码至少 8 位' in index_source
    assert '后续将加入币种风险预检' not in index_source
    assert '启动模拟' not in index_source
    assert '"token=" + encodeURIComponent(TOKEN)' not in index_source
    assert 'document.getElementById("lpass").value = "";' in index_source
    assert 'strategy_data = _apply_customer_profile_defaults({' in routes_source
    assert 'WHERE id=? AND user_id=?' in routes_source

    multi_path = index_path.parent / "multi.html"
    if multi_path.exists():
        multi_source = multi_path.read_text(encoding="utf-8")
        assert '"token=" + encodeURIComponent(TOKEN)' not in multi_source
        assert 'document.getElementById("pass").value = "";' in multi_source
    print("customer live path checks passed")


if __name__ == "__main__":
    main()


def test_customer_live_path():
    main()
