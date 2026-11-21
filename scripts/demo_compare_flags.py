# -*- coding: utf-8 -*-
"""安全核查：对比 flag=0(模拟) 与 flag=1(实盘) 账户余额，确认模拟头真实生效。"""
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
    cm.set_password(load_api_password_secure())
    keys = cm.load()

    import okx
    print("okx sdk version:", getattr(okx, "__version__", "?"))

    from okx.Account import AccountAPI

    for flag in ("0", "1"):
        acc = AccountAPI(keys["api_key"], keys["api_secret"], keys["passphrase"], flag=flag)
        bal = acc.get_account_balance()
        if bal.get("code") == "0":
            d = (bal.get("data") or [{}])[0]
            print("flag=%s -> code=0 totalEq=%s availEq=%s" % (
                flag, d.get("totalEq"), d.get("availEq")))
        else:
            print("flag=%s -> code=%s msg=%s" % (flag, bal.get("code"), bal.get("msg")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
