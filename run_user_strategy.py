#!/usr/bin/env python3
"""
用户策略运行入口 —— 被 ProcessManager 调用启动
从数据库读取用户策略配置和API密钥，创建独立运行实例
"""
import sys, os, json, time, argparse, base64
from pathlib import Path

PROJ = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJ))


def load_tested_profile(symbol: str) -> dict:
    import yaml

    symbol = (symbol or "").strip().upper()
    paths = [PROJ / "config" / "config.yaml"]
    paths.extend(sorted((PROJ / "config").glob("config_*.yaml")))
    paths.extend(sorted((PROJ / "config" / "standby").glob("config_*.yaml")))
    for path in paths:
        with open(path, encoding="utf-8") as f:
            profile = yaml.safe_load(f) or {}
        if str(profile.get("futures", {}).get("symbol", "")).upper() == symbol:
            return profile
    raise ValueError(f"customer strategy profile not found: {symbol}")


def build_user_config(config: dict, user_dir: Path) -> dict:
    profile = load_tested_profile(config.get("symbol", ""))
    yaml_cfg = json.loads(json.dumps(profile))
    paper_mode = bool(config.get("paper_mode", True))
    okx_flag = "1" if paper_mode else "0"

    yaml_cfg["user_data_dir"] = str(user_dir)
    yaml_cfg.setdefault("global", {}).update({
        "demo": paper_mode,
        "enable_futures": True,
        "enable_spot": False,
    })
    yaml_cfg.setdefault("customer", {})["capital_budget_usdt"] = float(config.get("capital_budget_usdt", 0) or 0)
    yaml_cfg.setdefault("exchange", {})["okx"] = {
        "flag": okx_flag,
        "key_file": str(user_dir / "keys.enc"),
        "proxy": config.get("proxy", ""),
    }
    yaml_cfg.setdefault("logging", {})["file"] = str(user_dir / "trading.log")
    return yaml_cfg


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-id", type=int, required=True)
    parser.add_argument("--user-dir", type=str, required=True)
    parser.add_argument("--log-file", type=str, default="")
    args = parser.parse_args()

    user_dir = Path(args.user_dir)
    cfg_file = user_dir / "config.json"
    key_file = user_dir / "keys.json"

    # 读取配置
    if not cfg_file.exists():
        print(f"[错误] 策略配置不存在: {cfg_file}")
        sys.exit(1)
    with open(cfg_file) as f:
        config = json.load(f)

    # 读取API密钥：优先使用一次性环境变量，兼容旧 keys.json 并立即删除。
    key_blob = os.environ.pop("OKX_USER_API_KEY_B64", "")
    if key_blob:
        try:
            keys = json.loads(base64.b64decode(key_blob.encode()).decode())
        except Exception as exc:
            print(f"[错误] API密钥环境变量解析失败: {type(exc).__name__}")
            sys.exit(1)
    else:
        if not key_file.exists():
            print(f"[错误] API密钥未提供")
            sys.exit(1)
        with open(key_file) as f:
            keys = json.load(f)
        try:
            key_file.unlink(missing_ok=True)
        except Exception:
            pass

    # 写入 config.yaml (main.py 需要 yaml 格式)
    import yaml
    yaml_cfg = build_user_config(config, user_dir)
    okx_flag = yaml_cfg["exchange"]["okx"]["flag"]
    yaml_path = user_dir / "config.yaml"
    with open(yaml_path, "w") as f:
        yaml.dump(yaml_cfg, f, default_flow_style=False)

    # 使用 CredentialManager 加密保存API密钥
    from api.crypto_utils import CredentialManager
    cm = CredentialManager(yaml_cfg)
    pw_path = user_dir / "api_pwd.txt"
    if pw_path.exists():
        pw = pw_path.read_text().strip()
    else:
        # 生成随机密码并存储
        import secrets
        pw = secrets.token_hex(16)
        pw_path.write_text(pw)
    cm.set_password(pw)
    cm.save(keys["api_key"], keys["api_secret"], keys["passphrase"])
    print(f"[OK] API密钥已加密保存到 {yaml_cfg['exchange']['okx']['key_file']}")

    # 设置日志路径
    if args.log_file:
        log_path = Path(args.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # 导入并启动引擎
    print(f"[OK] 启动用户策略 #{args.strategy_id} - {config.get('symbol', '?')}")
    from core.engine import MainEngine
    engine = MainEngine(str(yaml_path), {
        "api_key": keys["api_key"],
        "api_secret": keys["api_secret"],
        "passphrase": keys["passphrase"],
        "flag": okx_flag
    })
    engine.start()
