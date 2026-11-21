"""固化「止盈覆盖只统计有交易所订单号支撑的层」的行为。

背景（2026-11-03 实盘事故）：
FARTCOIN 空单 934 张、RAM 空单 76.7 张的止盈覆盖长期为 0，导致
tp_coverage_incomplete 在 24 小时内拦下 793 次加空，空边彻底冻死。

根因是 _place_tp_order 计算已覆盖数量时完全采信本地 _layer_qty_map。
一旦其中残留了「本地有记录、交易所无订单」的幽灵层（挂单中途失败、
撤单/成交后本地未清理），覆盖量就被虚报到等于持仓量，available_qty 恒为 0，
补挂必然以 qty_below_min 失败 —— 覆盖因此永远补不回来。

修复语义：
- 只有 _layer_tp_orders 中存在订单号的层才算真实覆盖。
- 无订单号的层视为幽灵，不计入；但结果以交易所实际覆盖量为下限，
  避免丢弃幽灵后低估覆盖、超额提交 reduce-only 数量。
- 交易所覆盖视图不可用时退回纯本地口径（fail-closed，与旧版一致）。
"""
import types

import pytest

from strategies.hedge_kit.tp import TpMixin


class _Log:
    def __init__(self):
        self.warnings = []

    def info(self, *_a, **_kw):
        pass

    def warning(self, msg, *_a, **_kw):
        self.warnings.append(str(msg))

    def error(self, *_a, **_kw):
        pass


def _stub(
    *,
    qty_map,
    tp_orders,
    exchange_ready=True,
    exchange_covered=0.0,
):
    """最小替身：只提供 _tp_covered_other_qty 读取的属性。"""
    s = types.SimpleNamespace()
    s.log = _Log()
    s._symbol = "FARTCOIN-USDT-SWAP"
    s._layer_qty_map = dict(qty_map)
    s._layer_tp_orders = dict(tp_orders)
    s._layer_side_map = {}
    s._exchange_tp_coverage_ready = exchange_ready
    s._exchange_tp_coverage_qty = {"long": 0.0, "short": exchange_covered}
    # 真实方法绑定到替身上
    s._tp_covered_other_qty = types.MethodType(TpMixin._tp_covered_other_qty, s)
    s._risk_tp_covered_qty = types.MethodType(TpMixin._risk_tp_covered_qty, s)
    s._tp_covered_qty = lambda side: sum(
        abs(v) for k, v in s._layer_qty_map.items() if s._tp_lid_side(k) == side
    )
    s._tp_lid_side = lambda lid: s._layer_side_map.get(str(lid), "short")
    return s


def test_live_layers_count_as_coverage():
    """有订单号的层照旧计入覆盖，行为与修复前一致。"""
    s = _stub(
        qty_map={"l1": 10.0, "l2": 5.0},
        tp_orders={"l1": "oid-1", "l2": "oid-2"},
        exchange_ready=False,
    )
    s._layer_side_map = {"l1": "short", "l2": "short"}
    assert s._tp_covered_other_qty("short", "l9", 20.0, 1e-6) == 15.0


def test_phantom_layer_is_not_counted():
    """无订单号的幽灵层不得计入覆盖 —— 这是死锁的根因。"""
    s = _stub(
        qty_map={"l1": 19.1},
        tp_orders={},  # 本地预留但交易所没有订单
        exchange_ready=True,
        exchange_covered=0.0,
    )
    s._layer_side_map = {"l1": "short"}
    assert s._tp_covered_other_qty("short", "l9", 19.1, 1e-6) == 0.0
    assert any("phantom coverage ignored" in w for w in s.log.warnings)


def test_phantom_freed_quantity_can_be_placed():
    """幽灵被剔除后，可用数量恢复到持仓量，补挂不再撞 qty_below_min。"""
    s = _stub(
        qty_map={"l1": 19.1},
        tp_orders={},
        exchange_ready=True,
        exchange_covered=0.0,
    )
    s._layer_side_map = {"l1": "short"}
    live_qty = 19.1
    covered = s._tp_covered_other_qty("short", "l9", live_qty, 1e-6)
    available = max(0.0, live_qty - covered)
    assert available == pytest.approx(19.1)


def test_exchange_coverage_floors_the_result():
    """交易所确实有覆盖时，剔除幽灵不得低估覆盖（防超额提交）。"""
    s = _stub(
        qty_map={"l1": 19.1},
        tp_orders={},  # 本地全是幽灵
        exchange_ready=True,
        exchange_covered=12.0,  # 交易所实际还挂着 12 张
    )
    s._layer_side_map = {"l1": "short"}
    assert s._tp_covered_other_qty("short", "l9", 19.1, 1e-6) == pytest.approx(12.0)


def test_exchange_view_unavailable_falls_back_to_local():
    """交易所视图不可用时退回本地口径，保持 fail-closed。"""
    s = _stub(
        qty_map={"l1": 10.0},
        tp_orders={"l1": "oid-1"},
        exchange_ready=False,
    )
    s._layer_side_map = {"l1": "short"}
    assert s._tp_covered_other_qty("short", "l9", 20.0, 1e-6) == 10.0


def test_excluded_lid_and_other_side_ignored():
    """本层自身与另一方向的层都不参与统计。"""
    s = _stub(
        qty_map={"self": 7.0, "same": 3.0, "other": 100.0},
        tp_orders={"self": "oid-s", "same": "oid-a", "other": "oid-o"},
        exchange_ready=False,
    )
    s._layer_side_map = {"self": "short", "same": "short", "other": "long"}
    assert s._tp_covered_other_qty("short", "self", 50.0, 1e-6) == 3.0


def test_mixed_live_and_phantom():
    """真实层计入、幽灵层剔除，两者混合时只算真实部分。"""
    s = _stub(
        qty_map={"live": 8.0, "ghost": 11.1},
        tp_orders={"live": "oid-live"},
        exchange_ready=True,
        exchange_covered=8.0,
    )
    s._layer_side_map = {"live": "short", "ghost": "short"}
    assert s._tp_covered_other_qty("short", "l9", 19.1, 1e-6) == pytest.approx(8.0)


def test_rehang_replace_excludes_cancelling_layer():
    """rehang 替换某层时，交易所缓存仍把正在撤销的旧单算进覆盖，必须靠
    exclude_qty 把这层减掉，否则 available 被压到 0、补挂必 qty_below_min。

    复现实盘事故：FARTCOIN 空单 906 = 272+453+181，rehang 撤掉 181 那层并立即
    再挂，但交易所缓存(_exchange_tp_coverage_qty)尚未刷新、仍含 181 旧单，于是
    exchange_covered=906，available = 906-906 = 0。修复后减去 exclude_qty=181，
    available 应正好等于被替换层的量(181)。
    """
    s = _stub(
        qty_map={"l2": 272.0, "l3": 453.0},        # 其它两层仍在本地
        tp_orders={"l2": "oid-2", "l3": "oid-3"},  # 且有订单号
        exchange_ready=True,
        exchange_covered=906.0,                      # 缓存仍含 181 旧单
    )
    s._layer_side_map = {"l2": "short", "l3": "short"}
    live_qty = 906.0
    # 替换层 lid="l1"(已被 rehang 从本地 pop)，其旧单量=181 经 exclude_qty 传入
    covered = s._tp_covered_other_qty("short", "l1", live_qty, 1e-6, exclude_qty=181.0)
    assert covered == pytest.approx(725.0)
    available = max(0.0, live_qty - covered)
    assert available == pytest.approx(181.0)  # 恰好等于被替换层的量，补挂不再撞下限


def test_rehang_replace_default_zero_keeps_supplement_behavior():
    """不带 exclude_qty(默认0)时行为不变，保证 supplement/seed 不被误改。"""
    s = _stub(
        qty_map={"l2": 272.0, "l3": 453.0},
        tp_orders={"l2": "oid-2", "l3": "oid-3"},
        exchange_ready=True,
        exchange_covered=906.0,
    )
    s._layer_side_map = {"l2": "short", "l3": "short"}
    # 默认 exclude_qty=0：仍被缓存顶到 906，available=0（这正是 supplement 不该减的原因）
    covered = s._tp_covered_other_qty("short", "l9", 906.0, 1e-6)
    assert covered == pytest.approx(906.0)
    assert max(0.0, 906.0 - covered) == pytest.approx(0.0)
