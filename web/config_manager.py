"""Web面板配置文件管理"""
import json
import os
import yaml
import hashlib
import secrets
from utils.crypto import UserCrypto

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(CONFIG_DIR)
WEB_CONFIG_PATH = os.path.join(CONFIG_DIR, "web_config.json")
ENGINE_CONFIG_PATH = os.path.join(PROJECT_DIR, "config", "config.yaml")
KEYS_PATH = os.path.join(PROJECT_DIR, "data", "keys.enc")
CRYPTO_PATH = os.path.join(PROJECT_DIR, "api", "crypto_utils.py")

def _default_web_config():
    return {
        "auth": {
            "username": "admin",
            "password_hash": "",
            "session_token": "",
            "users": {},
        },
        "server": {"host": "0.0.0.0", "port": 8080},
        "web_log": "logs/web.log",
    }

def load_web_config():
    if not os.path.exists(WEB_CONFIG_PATH):
        return _default_web_config()
    with open(WEB_CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("auth", {})
    cfg["auth"].setdefault("username", "admin")
    cfg["auth"].setdefault("password_hash", "")
    cfg["auth"].setdefault("session_token", "")
    cfg["auth"].setdefault("users", {})
    cfg.setdefault("server", {"host": "0.0.0.0", "port": 8080})
    cfg.setdefault("web_log", "logs/web.log")
    return cfg

def save_web_config(cfg):
    os.makedirs(os.path.dirname(WEB_CONFIG_PATH), exist_ok=True)
    with open(WEB_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def load_engine_config():
    with open(ENGINE_CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def save_engine_config(cfg):
    with open(ENGINE_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

def hash_password(password):
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return salt.hex() + ":" + key.hex()

def verify_password(password, stored):
    if not stored or ":" not in stored:
        return False
    salt_hex, key_hex = stored.split(":")
    salt = bytes.fromhex(salt_hex)
    stored_key = bytes.fromhex(key_hex)
    new_key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return new_key == stored_key

def generate_token():
    return secrets.token_hex(32)


def save_api_password_secure(password: str):
    cfg = load_web_config()
    cfg["api_password_enc"] = UserCrypto.encrypt(password)
    cfg.pop("api_password", None)
    save_web_config(cfg)


def load_api_password_secure() -> str:
    cfg = load_web_config()
    encrypted = str(cfg.get("api_password_enc") or "")
    if not encrypted:
        return ""
    return UserCrypto.decrypt(encrypted)

def test_api_connection(api_key, api_secret, passphrase, flag="0"):
    """测试OKX API连接（支持新旧SDK版本）"""
    try:
        try:
            # 新版SDK (v2.5+)
            from okx import Account
            acct = Account(api_key, api_secret, passphrase, flag=flag)
            resp = acct.get_account_balance()
        except (ImportError, AttributeError):
            # 旧版SDK
            from okx import api as okx_api
            acct = okx_api.Account(api_key, api_secret, passphrase, flag=flag)
            try:
                resp = acct.get_account_balance()
            except AttributeError:
                resp = acct.get_account()
        return resp.get("code") == "0", resp.get("msg", "")
    except Exception as e:
        return False, str(e)
