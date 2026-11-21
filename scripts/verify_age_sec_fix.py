# -*- coding: utf-8 -*-
"""验证 account_risk_age_sec 修复：
- 快照带 observed_at_ms  -> 返回数值
- 快照缺 observed_at_ms 或为 0 -> 返回 None（修复前会 round(None,1) 抛 TypeError）
字段位于 build_status 返回的 monitor 嵌套字典内。
"""
import sys

sys.path.insert(0, "/opt/okx_quant")
sys.path.insert(0, "/opt/okx_quant/scripts")
import compare_status_split as cs  # noqa: E402
from strategies.hedge_status import build_status  # noqa: E402


def age_of(s):
    out = build_status(s)
    monitor = out.get("monitor", out)
    assert "account_risk_age_sec" in monitor, "key missing in %r" % list(out)[:10]
    return monitor["account_risk_age_sec"]


def main():
    s = cs.build_strategy()
    v_ok = age_of(s)
    assert isinstance(v_ok, (int, float)) and v_ok >= 0, repr(v_ok)

    for bad in (None, 0, "0"):
        snap = dict(s._account_risk_snapshot)
        if bad is None:
            snap.pop("observed_at_ms", None)
        else:
            snap["observed_at_ms"] = bad
        s._account_risk_snapshot = snap
        v_none = age_of(s)
        assert v_none is None, "bad=%r -> %r" % (bad, v_none)

    print("RESULT: PASS — normal=%.2f, missing/zero -> None" % v_ok)
    return 0


if __name__ == "__main__":
    sys.exit(main())
