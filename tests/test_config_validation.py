from pathlib import Path

import pytest
import yaml

from core.config_validation import validate_config_bundle


ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return yaml.safe_load((ROOT / "config" / name).read_text(encoding="utf-8"))


def test_live_profiles_have_explicit_repair_boundaries():
    extras = [
        (f"config/{name}", _load(name))
        for name in ("config_dram.yaml", "config_fartcoin.yaml")
        if (ROOT / "config" / name).exists()
    ]
    result = validate_config_bundle(
        _load("config.yaml"),
        "config/config.yaml",
        extras,
    )
    assert result["ok"], result["errors"]
    expected_symbols = {"CRV-USDT-SWAP", "FARTCOIN-USDT-SWAP"}
    if (ROOT / "config" / "config_dram.yaml").exists():
        expected_symbols.add("DRAM-USDT-SWAP")
    assert {row["symbol"] for row in result["profiles"]} == expected_symbols


def test_xau_profile_is_valid_and_isolated():
    if not (ROOT / "config" / "config_xau.yaml").exists():
        pytest.skip("config_xau.yaml 已停用（XAU 策略退役，仅存 standby 归档）")
    config = _load("config_xau.yaml")
    result = validate_config_bundle(config, "config/config_xau.yaml")

    assert result["ok"], result["errors"]
    assert result["profiles"][0]["symbol"] == "XAU-USDT-SWAP"
    assert result["profiles"][0]["runtime_name"] == "futures_xau"
    assert config["global"]["demo"] is False
    assert config["exchange"]["okx"]["flag"] == "0"


def test_eth_profile_is_valid_and_isolated():
    # ETH 已于 2026-10-21 按用户要求停用：活动配置改名为 config_eth.yaml.disabled。
    # 仍对归档文件做完整校验，保证将来重新启用时配置本身合法。
    name = "config_eth.yaml"
    if not (ROOT / "config" / name).exists():
        name = "config_eth.yaml.disabled"
    if not (ROOT / "config" / name).exists():
        pytest.skip("config_eth.yaml 已停用且归档缺失")
    config = _load(name)
    result = validate_config_bundle(config, "config/" + name)

    assert result["ok"], result["errors"]
    assert result["profiles"][0]["symbol"] == "ETH-USDT-SWAP"
    assert result["profiles"][0]["runtime_name"] == "futures_eth"
    assert config["futures"]["add_step"] == 0.0035
    assert config["futures"]["max_add_layers"] == 3
    assert config["futures"]["max_long_side_notional_usdt"] == 250
    assert config["futures"]["max_short_side_notional_usdt"] == 250
    assert config["futures"]["max_total_notional_usdt"] == 500
    assert config["futures"]["max_net_notional_soft_usdt"] == 80
    assert config["futures"]["max_net_notional_usdt"] == 150


def test_demo_and_okx_flag_must_match():
    config = _load("config.yaml")
    config["global"]["demo"] = True
    config["exchange"]["okx"]["flag"] = "0"
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("global.demo=true requires" in item for item in result["errors"])

    config["exchange"]["okx"]["flag"] = "1"
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"], result["errors"]


def test_live_mode_rejects_demo_flag():
    config = _load("config.yaml")
    config["global"]["demo"] = False
    config["exchange"]["okx"]["flag"] = "1"
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("global.demo=false requires" in item for item in result["errors"])


def test_missing_repair_boundary_is_a_startup_error():
    config = _load("config.yaml")
    config["futures"].pop("inventory_repair_max_margin_ratio")
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("inventory_repair_max_margin_ratio must be explicit" in item for item in result["errors"])


def test_repair_boundary_cannot_reach_hard_margin_line():
    config = _load("config.yaml")
    config["futures"]["inventory_repair_max_margin_ratio"] = config["futures"]["account_margin_hard_limit"]
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("must be below hard_limit" in item for item in result["errors"])


def test_crv_hard_margin_override_is_explicit_and_cannot_be_copied_to_another_symbol():
    config = _load("config.yaml")
    config["futures"]["inventory_repair_hard_override_enabled"] = True
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"], result["errors"]

    config["futures"].pop("inventory_repair_hard_override_max_step_notional_usdt")
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("must be explicit when override is enabled" in item for item in result["errors"])

    other = _load("config_fartcoin.yaml")
    other["futures"].update({
        "inventory_repair_hard_override_enabled": True,
        "inventory_repair_hard_override_symbol": "CRV-USDT-SWAP",
        "inventory_repair_hard_override_margin_ratio": 0.63,
        "inventory_repair_hard_override_mgn_ratio_min": 12.0,
        "inventory_repair_hard_override_max_step_notional_usdt": 8.0,
    })
    result = validate_config_bundle(other, "config/config_fartcoin.yaml")
    assert result["ok"] is False
    assert any("restricted to CRV-USDT-SWAP" in item for item in result["errors"])


def test_crv_hard_margin_override_must_remain_narrower_than_the_account():
    config = _load("config.yaml")
    config["futures"]["inventory_repair_hard_override_enabled"] = True
    config["futures"]["inventory_repair_hard_override_margin_ratio"] = 0.70
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is True

    config["futures"]["inventory_repair_hard_override_margin_ratio"] = 0.30
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("must be above hard_limit" in item for item in result["errors"])

    config = _load("config.yaml")
    config["futures"]["inventory_repair_hard_override_enabled"] = True
    # upper bound = min(25.0, max_net_notional_usdt * 0.10); 30.0 is above the
    # hard 25.0 ceiling for any net cap, so this stays an out-of-range probe.
    config["futures"]["inventory_repair_hard_override_max_step_notional_usdt"] = 30.0
    result = validate_config_bundle(config, "config/config.yaml")
    assert result["ok"] is False
    assert any("max_step_notional_usdt" in item for item in result["errors"])


def test_live_profiles_recover_below_sixty_and_keep_grid_gate_at_hard_line():
    profiles = [
        _load(name)
        for name in ("config.yaml", "config_dram.yaml", "config_fartcoin.yaml")
        if (ROOT / "config" / name).exists()
    ]
    for config in profiles:
        futures = config["futures"]
        hard = futures["account_margin_hard_limit"]
        resume = futures["account_margin_resume_limit"]
        assert 0 < resume < hard <= 0.60
        # The grid expansion gate must stay below the hard margin line; the
        # exact value is tunable (0.20 during the tight phase, 0.28 after
        # the CRV/ETH capacity release), so assert the relationship only.
        assert futures["grid_expand_margin_ratio"] < futures["account_margin_hard_limit"]
        assert futures["account_margin_hard_limit"] - futures["grid_expand_margin_ratio"] >= 0.015

def test_fart_hard_margin_override_requires_explicit_fart_profile_opt_in():
    fart = _load("config_fartcoin.yaml")
    fart["futures"].update({
        "inventory_repair_hard_override_enabled": True,
        "inventory_repair_hard_override_symbol": "FARTCOIN-USDT-SWAP",
        "inventory_repair_hard_override_margin_ratio": 0.63,
        "inventory_repair_hard_override_mgn_ratio_min": 12.0,
        "inventory_repair_hard_override_max_step_notional_usdt": 8.0,
    })
    result = validate_config_bundle(fart, "config/config_fartcoin.yaml")
    assert result["ok"], result["errors"]

def test_fart_hard_margin_override_accepts_conservative_73_percent_cap():
    fart = _load("config_fartcoin.yaml")
    fart["futures"]["inventory_repair_hard_override_margin_ratio"] = 0.73
    result = validate_config_bundle(fart, "config/config_fartcoin.yaml")
    assert result["ok"], result["errors"]
