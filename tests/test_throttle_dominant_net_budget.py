"""固化「THROTTLED 模式仅封主导边、但主导边预算未耗尽时放行」的行为。

核心语义：
- 账户进入 THROTTLED（soft <= load < hard）时，只封本品种的主导边。
- 开启 throttle_dominant_net_budget_gate_enabled 后，若本品种净敞口仍小于
  max_net_notional_soft_usdt（自身预算未花完），主导边不再被账户级节流封死。
- 净敞口已达到/超过自身软限，或没有软限配置、没有可用标记价时，
  保持旧的封锁行为（fail-closed）。
- 开关关闭（默认）时行为与旧版完全一致。
"""
import types

from strategies.futures_hedge import FuturesHedgeStrategy


class _Log:
    def info(self, *_a, **_kw):
        pass

    def warning(self, *_a, **_kw):
        pass


def _stub(
    *,
    margin_load=0.538,
    dominant="short",
    net_notional=238.0,
    soft=300.0,
    gate=True,
    mark_price=9.858,
    soft_limit=0.40,
    hard_limit=0.55,
):
    """最小 Strategy 替身：只提供被这两个方法读取的属性。"""
    s = types.SimpleNamespace()
    s._symbol = "RAM-USDT-SWAP"
    s.log = _Log()
    s._account_margin_soft_limit = soft_limit
    s._account_margin_hard_limit = hard_limit
    s._max_net_notional_soft_usdt = soft
    s._throttle_dominant_net_budget_gate_enabled = gate
    s._last_price = mark_price
    s._last_throttle_release_log_ts = 0.0

    s._account_margin_load_ratio = lambda: margin_load
    s._dominant_side = lambda: dominant
    s._net_notional_exposure = lambda price=0.0: net_notional
    # 纯方法需要真实定义（替身本身不是 Strategy 实例）
    s._throttle_dominant_net_budget_released = types.MethodType(
        FuturesHedgeStrategy._throttle_dominant_net_budget_released, s
    )
    return s


def _blocked(stub, side):
    return FuturesHedgeStrategy._portfolio_throttled_dominant_side_blocked(stub, side)


def test_one_sided_symbol_released_while_inside_its_own_soft_cap():
    """RAM 现场复现：净空 238U < 软限 300U，主导边是 short，应放行。"""
    s = _stub()
    assert _blocked(s, "short") is False


def test_dominant_side_still_blocked_once_own_cap_is_spent():
    """净敞口达到自身软限后，主导边恢复封锁。"""
    s = _stub(net_notional=300.0)
    assert _blocked(s, "short") is True
    s2 = _stub(net_notional=420.0)
    assert _blocked(s2, "short") is True


def test_weak_side_is_never_blocked():
    """非主导边本来就不受影响。"""
    s = _stub(dominant="short")
    assert _blocked(s, "long") is False


def test_gate_off_keeps_legacy_veto():
    """开关关闭时保持旧行为：主导边被封锁（向后兼容）。"""
    s = _stub(gate=False)
    assert _blocked(s, "short") is True


def test_active_mode_never_blocks():
    """冻结率低于软线（ACTIVE）时不封锁任何边。"""
    s = _stub(margin_load=0.20)
    assert _blocked(s, "short") is False


def test_hard_mode_is_not_handled_here():
    """HARD 模式不由本方法封锁（走全量开仓否决通道）。"""
    s = _stub(margin_load=0.60)
    assert _blocked(s, "short") is False


def test_missing_soft_cap_fails_closed():
    """未配置软限（CRV 旧配置情形）不放行，保持封锁。"""
    s = _stub(soft=0.0)
    assert _blocked(s, "short") is True


def test_missing_mark_price_fails_closed():
    """没有标记价时无法度量净敞口，保守封锁。"""
    s = _stub(mark_price=0.0)
    assert _blocked(s, "short") is True


def test_crv_shape_still_blocked():
    """CRV 现场：净多 1303U 远超软限 1100U，主导边 long 仍封锁。"""
    s = _stub(dominant="long", net_notional=1303.0, soft=1100.0,
              mark_price=0.3386)
    assert _blocked(s, "long") is True
