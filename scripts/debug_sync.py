# -*- coding: utf-8 -*-
"""调试：AccountRiskState.sync 在模拟盘账户下的行为与余额原始结构。"""
import sys

sys.path.insert(0, "/opt/okx_quant")
sys.path.insert(0, "/opt/okx_quant/web")

import yaml  # noqa: E402
import json  # noqa: E402


def main():
    config = yaml.safe_load(open("/opt/okx_quant/config/config.yaml", encoding="utf-8"))
    from config_manager import load_api_password_secure  # noqa: E402
    from api.crypto_utils import CredentialManager  # noqa: E402
    from api.okx_client import OKXClient  # noqa: E402
    from risk.account_state import AccountRiskState  # noqa: E402

    cm = CredentialManager(config)
    cm.set_password(load_api_password_secure())
    keys = cm.load()
    flag = config["exchange"]["okx"].get("flag", "1")

    client = OKXClient(keys, flag=flag)
    raw = client.get_account_balance_raw()
    print("raw code:", raw.get("code"), "msg:", raw.get("msg"))
    for d in (raw.get("data") or []):
        print("data item keys:", list(d.keys()))
        print("totalEq:", d.get("totalEq"), "availEq:", d.get("availEq"), "mgnRatio:", d.get("mgnRatio"))
        for det in d.get("details", []):
            print("  detail:", json.dumps({k: det.get(k) for k in ("ccy", "eq", "availBal", "frozenBal", "mgnRatio")}))
    st = AccountRiskState(client, None)
    ok = st.sync(force=True)
    print("sync() ->", ok)
    print("cached: avail=%s frozen=%s equity=%s" % (st.cached_avail, st.cached_frozen, st.cached_equity))
    snap = getattr(st, "snapshot", None)
    print("snapshot:", json.dumps(snap, default=str)[:300] if snap else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
