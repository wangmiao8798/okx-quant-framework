"""固化「降净敞口方向豁免保证金硬线」的行为。

核心语义：
- 当 account.net_reducing_hard_margin_exempt_enabled=True 时，降低净敞口的
  加仓（净多时加空 / 净空时加多）不再被 account_hard_margin_limit 拦；
  扩大净敞口的加仓仍照拦。
- 开关关闭（默认）时行为与旧版一致（fail-closed），普通加仓照拦。
"""
import time

from risk.policy import RiskPolicy


def _ctx(long_qty=0.0, short_qty=0.0, margin_load=0.60, exempt=False):
    # margin_load 0.60 已超过 hard_limit 0.55，确保硬线处于触发态。
    return {
        "context_version": 1,
        "generated_at": time.time(),
        "symbol": "CRV-USDT-SWAP",
        "positions": {
            "long_qty": long_qty,
            "short_qty": short_qty,
            "max_net_exposure": 0.0,
            "max_net_notional_usdt": 10000.0,
        },
        "account": {
            "margin_load_ratio": margin_load,
            "account_margin_soft_limit": 0.45,
            "account_margin_hard_limit": 0.55,
            "account_margin_resume_limit": 0.50,
            "account_margin_scope": "test_net_reducing",
            "hard_margin_ratio": 0.55,
            "paired_net_neutral_max_margin_ratio": 0.48,
            "mgn_ratio_open_limit": 5.0,
            "net_reducing_hard_margin_exempt_enabled": exempt,
            "mark_price": 100.0,
            "contract_value": 1.0,
        },
        "account_risk": {"usdt": {"mgnRatio": "30"}},
    }


def _eval(ctx, side, pos_side, qty):
    return RiskPolicy.evaluate_order(
        "ACTIVE", ctx["symbol"], side, pos_side, False,
        payload={"quantity": qty, "px": "100.0"},
        reconciler_status={"ready": True, "last_success": time.time()},
        context=ctx,
    )


def test_net_long_add_short_exempts_hard_line_when_opted_in():
    RiskPolicy._hard_margin_latches.clear()
    # 净多 5000，加空 100（sell short）→ 降净敞口 → 豁免
    ctx = _ctx(long_qty=5000.0, short_qty=0.0, exempt=True)
    r = _eval(ctx, "sell", "short", "100")
    assert r["reason_code"] == "active"
    assert "account_hard_margin_limit" not in r["shadow"]["reason_codes"]
    assert r["shadow"]["metrics"]["net_after"] < r["shadow"]["metrics"]["net_before"]


def test_net_long_add_long_still_blocked_when_opted_in():
    RiskPolicy._hard_margin_latches.clear()
    # 净多 5000，加多 100（buy long）→ 扩净敞口 → 仍应被硬线拦
    ctx = _ctx(long_qty=5000.0, short_qty=0.0, exempt=True)
    r = _eval(ctx, "buy", "long", "100")
    assert r["reason_code"] == "account_hard_margin_limit"


def test_net_reducing_still_blocked_when_opt_in_disabled():
    RiskPolicy._hard_margin_latches.clear()
    # 开关关闭（默认），加空也应被硬线拦（fail-closed 保持）
    ctx = _ctx(long_qty=5000.0, short_qty=0.0, exempt=False)
    r = _eval(ctx, "sell", "short", "100")
    assert r["reason_code"] == "account_hard_margin_limit"


def test_net_short_add_long_exempts_symmetrically():
    RiskPolicy._hard_margin_latches.clear()
    # 净空 5000，加多 100（buy long）→ 降净敞口（对称方向）
    ctx = _ctx(long_qty=0.0, short_qty=5000.0, exempt=True)
    r = _eval(ctx, "buy", "long", "100")
    assert r["reason_code"] == "active"
    assert "account_hard_margin_limit" not in r["shadow"]["reason_codes"]
