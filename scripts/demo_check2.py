# -*- coding: utf-8 -*-
"""调试：检查 CredentialManager 加载结果与密钥文件结构。"""
import sys

sys.path.insert(0, "/opt/okx_quant")
sys.path.insert(0, "/opt/okx_quant/web")

import yaml  # noqa: E402


def mask(v):
    if v is None:
        return None
    s = str(v)
    return s[:4] + "..." if len(s) > 8 else "***"


def main():
    config = yaml.safe_load(open("/opt/okx_quant/config/config.yaml", encoding="utf-8"))
    from config_manager import load_api_password_secure  # noqa: E402
    from api.crypto_utils import CredentialManager  # noqa: E402

    cm = CredentialManager(config)
    print("key_file:", cm.key_file if hasattr(cm, "key_file") else getattr(cm, "_key_file", "?"))
    print("exists:", cm.exists())
    pw = load_api_password_secure()
    print("web password available:", bool(pw))
    if pw:
        cm.set_password(pw)
        try:
            keys = cm.load()
            print("load() type:", type(keys).__name__)
            if isinstance(keys, dict):
                for k, v in keys.items():
                    print("  key:", k, "->", mask(v))
            else:
                print("  value:", mask(keys))
        except Exception as e:
            print("load() failed:", type(e).__name__, e)
    # 看密钥文件格式（非敏感：只打印头部字节结构）
    import os
    path = config.get("exchange", {}).get("okx", {}).get("key_file", "data/keys.enc")
    full = os.path.join("/opt/okx_quant", path)
    if os.path.exists(full):
        raw = open(full, "rb").read()
        print("key file size:", len(raw), "first bytes:", raw[:24].hex())
    return 0


if __name__ == "__main__":
    sys.exit(main())
