from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_profile_configs_keep_notification_passwords_out_of_yaml():
    profiles = [
        path
        for path in (
            ROOT / "config" / "config_dram.yaml",
            ROOT / "config" / "config_fartcoin.yaml",
            *sorted((ROOT / "config" / "standby").glob("config_*.yaml")),
        )
        if path.exists()
    ]

    for path in profiles:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        email = config.get("notify", {}).get("email", {})
        if not email:
            continue
        assert not email.get("password"), path
        assert email.get("password_env") == "OKX_NOTIFY_SMTP_PASSWORD", path
