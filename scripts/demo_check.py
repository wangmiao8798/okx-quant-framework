# -*- coding: utf-8 -*-
"""验证 OKX 模拟盘（flag=0）账户可访问性：解密密钥 → 查询模拟账户余额与持仓。"""
import sys

sys.path.insert(0, "/opt/okx_quant")
sys.path.insert(0, "/opt/okx_quant/web")

import yaml  # noqa: E402


def main():
    config = yaml.safe_load(open("/opt/okx_quant/config/config.yaml", encoding="utf-8"))
    from config_manager import load_api_password_secure  # noqa: E402
    from api.crypto_utils import CredentialManager  # noqa: E402

    cm = CredentialManager(config)
    pw = load_api_password_secure()
    if not pw:
        print("RESULT: no web password available")
        return 2
    cm.set_password(pw)
    keys = cm.load()
    print("keys loaded: api_key=%s... secret=*** passphrase=***" % keys["api_key"][:6])

    flag = config["exchange"]["okx"].get("flag", "1")
    print("configured flag:", flag)

    from okx.Account import AccountAPI  # noqa: E402

    acc = AccountAPI(keys["api_key"], keys["api_secret"], keys["passphrase"], flag=flag)
    bal = acc.get_account_balance()
    print("balance resp code:", bal.get("code"), "msg:", bal.get("msg"))
    if bal.get("code") == "0":
        data = bal.get("data", [{}])
        for d in data[:3]:
            print("  totalEq=%s availEq=%s details=%d" % (
                d.get("totalEq"), d.get("availEq"), len(d.get("details", []) or [])))
        pos = acc.get_positions()
        print("positions resp code:", pos.get("code"), "count:", len(pos.get("data", []) or []))
        for p in (pos.get("data") or [])[:5]:
            print("  ", p.get("instId"), p.get("posSide"), p.get("pos"), "upl=%s" % p.get("upl"))
        print("RESULT: DEMO ACCOUNT OK")
        return 0
    print("RESULT: demo account unreachable:", bal.get("code"), bal.get("msg"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
