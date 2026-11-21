# -*- coding: utf-8 -*-
"""只读纯计算函数集合，从 FuturesHedgeStrategy 提取（拆分第二阶段）。

设计约束（拆分契约）：
- 所有函数无副作用：不写策略状态、不下单、不查询外部、不触发持久化。
- 输入为显式参数/数据对象，输出为纯计算结果。
- 原策略方法保留为兼容代理，方法名与返回接口不变。
- 本模块不 import strategies.futures_hedge，避免循环依赖。
"""
from __future__ import annotations

import math
import time
from typing import Dict, Optional

from utils.helpers import safe_float


def side_value(side_config: Dict, side: str, key: str, default=None):
    """读取按边配置，带旧版共享回退（原 _side_value）。"""
    side = str(side or "").lower()
    cfg = (side_config or {}).get(side, {})
    if isinstance(cfg, dict) and key in cfg and cfg.get(key) is not None:
        return cfg.get(key)
    return default


def side_float(side_config: Dict, side: str, key: str, default: float) -> float:
    return safe_float(side_value(side_config, side, key, default))


def side_int(side_config: Dict, side: str, key: str, default: int) -> int:
    try:
        return int(safe_float(side_value(side_config, side, key, default)))
    except (TypeError, ValueError):
        return int(default)


def contract_qty_for_margin(
    margin_usdt: float,
    price: float,
    leverage: float,
    contract_value: float,
    qty_step: float,
    min_qty: float = 0.0,
) -> float:
    """Convert a margin budget to exchange contract quantity."""
    margin = max(0.0, safe_float(margin_usdt))
    px = max(0.0, safe_float(price))
    lev = max(1.0, safe_float(leverage, 1.0))
    ct_val = max(0.0, safe_float(contract_value))
    step = max(0.0, safe_float(qty_step))
    minimum = max(0.0, safe_float(min_qty))
    if margin <= 0.0 or px <= 0.0 or ct_val <= 0.0 or step <= 0.0:
        return 0.0
    qty = margin * lev / (px * ct_val)
    qty = round(qty / step) * step
    return max(qty, minimum)


def calc_pnl(
    long_qty: float, long_entry: float,
    short_qty: float, short_entry: float,
    price: float,
) -> float:
    """按双向持仓和最新价计算混合 PnL 比例（原 _calc_pnl）。"""
    if long_entry <= 0 or short_entry <= 0:
        return 0.0
    lp = (price - long_entry) / long_entry if long_qty > 0 else 0
    sp = (short_entry - price) / short_entry if short_qty > 0 else 0
    total = long_qty + short_qty
    return 0.0 if total == 0 else (lp * long_qty + sp * short_qty) / total


def net_imbalance_ratio(inventory) -> float:
    """库存净不平衡比例（原 _net_imbalance_ratio，委托 HedgeInventory）。"""
    return inventory.imbalance_ratio()


def dominant_side(inventory, min_qty: float) -> str:
    """主导持仓方向（原 _dominant_side，委托 HedgeInventory）。"""
    return inventory.dominant_side(min_qty)


def position_age_sec(
    position_first_seen: Optional[Dict], side: str, now: Optional[float] = None
) -> float:
    """持仓年龄秒数（原 _position_age_sec）。"""
    if not isinstance(position_first_seen, dict):
        return 0.0
    ts = safe_float(position_first_seen.get(side, 0.0))
    now = time.time() if now is None else now
    return max(0.0, now - ts) if ts > 0 else 0.0


def position_unrealized_pnl(
    side: str, last_price: float,
    long_qty: float, long_entry: float,
    short_qty: float, short_entry: float,
    contract_value: float = 1.0,
) -> float:
    """单边未实现盈亏 USDT（原 _position_unrealized_pnl）。"""
    if last_price <= 0:
        return 0.0
    ct_val = max(0.0, safe_float(contract_value))
    if ct_val <= 0:
        return 0.0
    if side == "long" and long_qty > 0 and long_entry > 0:
        return (last_price - long_entry) * long_qty * ct_val
    if side == "short" and short_qty > 0 and short_entry > 0:
        return (short_entry - last_price) * short_qty * ct_val
    return 0.0


def round_tp_price_away(side: str, price: float, tick_size: float) -> float:
    """止盈价按 tick 向外取整（原 _round_tp_price_away）。"""
    tick = max(0.0, safe_float(tick_size))
    if tick <= 0:
        return round(price, 12)
    if side == "long":
        units = math.ceil((price - tick * 1e-10) / tick)
    else:
        units = math.floor((price + tick * 1e-10) / tick)
    return round(units * tick, 12)


def effective_tp_ratio(
    target_ratio: float,
    min_edge_ratio: float,
    order_mode: str = "maker",
    taker_fee: float = 0.0005,
    maker_fee: float = 0.0002,
) -> float:
    """带手续费地板的有效止盈比例（原 _effective_tp_ratio）。"""
    if order_mode == "taker":
        fee_floor = taker_fee * 2 + 0.001
    else:
        fee_floor = taker_fee + maker_fee + 0.001
    return max(target_ratio, min_edge_ratio, fee_floor)


def effective_add_cooldown(cooldown: float, moca_state: str = "") -> float:
    """按 MOCA 状态调整的加仓冷却（原 _effective_add_cooldown）。"""
    cooldown = max(3.0, cooldown)
    if moca_state in {"calm", "strong_oscillation"}:
        return max(30.0, cooldown * 0.5)
    if moca_state == "weak_oscillation":
        return max(36.0, cooldown * 0.6)
    if moca_state in {"trending_up", "trending_down"}:
        return max(45.0, cooldown)
    if moca_state == "spike":
        return max(60.0, cooldown * 1.3)
    return cooldown


def inventory_net_size_multiplier(
    side: str,
    dominant_side: str,
    net_notional_usdt: float,
    soft_limit_usdt: float,
    hard_limit_usdt: float,
    min_multiplier: float = 0.10,
) -> float:
    """Scale the dominant side as net exposure grows between soft and hard limits."""
    side = str(side or "").lower()
    dominant_side = str(dominant_side or "").lower()
    if side not in {"long", "short"} or side != dominant_side:
        return 1.0
    soft = max(0.0, safe_float(soft_limit_usdt))
    hard = max(0.0, safe_float(hard_limit_usdt))
    net = abs(safe_float(net_notional_usdt))
    floor = min(1.0, max(0.0, safe_float(min_multiplier, 0.10)))
    if hard <= 0:
        return 1.0
    if soft <= 0 or soft >= hard:
        soft = hard * 0.5
    if net <= soft:
        return 1.0
    if net >= hard:
        return 0.0
    progress = (net - soft) / max(hard - soft, 1e-12)
    return max(floor, 1.0 - progress * (1.0 - floor))


def atr_adaptive_step(
    base_step: float,
    atr_ratio: float,
    enabled: bool,
    multiplier: float,
    min_step: float,
    max_step: float,
) -> float:
    """Return a volatility-scaled grid step with explicit bounds."""
    base = max(0.0, safe_float(base_step))
    if not enabled:
        return max(0.001, base)
    atr = max(0.0, safe_float(atr_ratio))
    mult = max(0.0, safe_float(multiplier))
    lower = max(0.0001, safe_float(min_step))
    upper = max(lower, safe_float(max_step))
    if atr <= 0 or mult <= 0:
        return max(lower, min(upper, base or lower))
    return max(lower, min(upper, atr * mult))


def hard_hedge_order_qty(
    abs_net_notional_usdt: float,
    soft_limit_usdt: float,
    hard_limit_usdt: float,
    price: float,
    contract_value: float,
    qty_step: float,
    min_qty: float,
    max_order_notional_usdt: float,
) -> float:
    """Size a weak-side hedge when net exposure breaches the hard limit."""
    net = abs(safe_float(abs_net_notional_usdt))
    soft = max(0.0, safe_float(soft_limit_usdt))
    hard = max(0.0, safe_float(hard_limit_usdt))
    px = max(0.0, safe_float(price))
    ct_val = max(0.0, safe_float(contract_value))
    step = max(0.0, safe_float(qty_step))
    minimum = max(0.0, safe_float(min_qty))
    max_notional = max(0.0, safe_float(max_order_notional_usdt))
    if net <= hard or px <= 0 or ct_val <= 0 or step <= 0:
        return 0.0
    target = soft if soft > 0 else hard * 0.5
    excess_notional = max(net - target, 0.0)
    if max_notional > 0:
        excess_notional = min(excess_notional, max_notional)
    if excess_notional <= 0:
        return 0.0
    raw_qty = math.ceil(excess_notional / (px * ct_val) / step) * step
    return max(raw_qty, minimum)


def effective_drift_threshold(drift_threshold: float, moca_state: str = "") -> float:
    """按 MOCA 状态调整的漂移阈值（原 _effective_drift_threshold）。"""
    if moca_state in {"calm", "strong_oscillation"}:
        return max(0.006, drift_threshold * 0.5)
    if moca_state == "weak_oscillation":
        return max(0.008, drift_threshold * 0.7)
    return drift_threshold


def effective_side_step(side_config: Dict, side: str, add_step: float) -> float:
    return max(0.001, side_float(side_config, side, "add_step", add_step))


def effective_side_multiplier(side_config: Dict, side: str, add_mult: float) -> float:
    return max(1.0, side_float(side_config, side, "add_multiplier", add_mult))


def effective_side_max_adds(side_config: Dict, side: str, max_adds: int) -> int:
    return max(1, min(20, side_int(side_config, side, "max_add_layers", max_adds)))


def effective_side_tp_ratio(
    side_config: Dict, side: str, which: str,
    first_tp: float, final_tp: float, trail: float,
) -> float:
    """按边 TP 基线，保留旧版默认（原 _effective_side_tp_ratio）。"""
    side = str(side or "").lower()
    if which == "final":
        fallback = final_tp
        key = "final_tp_ratio"
    elif which in {"trailing", "trail"}:
        fallback = trail
        key = "trailing_tp_retreat"
    else:
        fallback = first_tp
        key = "first_tp_ratio"
    value = side_float(side_config, side, key, fallback) if side in {"long", "short"} else fallback
    if which == "final":
        value = max(
            value,
            effective_side_tp_ratio(side_config, side, "first", first_tp, final_tp, trail) + 0.002,
        )
    return max(0.001, value)


def effective_base_pos_usdt(
    base_pos_usdt: float,
    side_config: Dict,
    side: str,
    in_recovery: bool,
    recovery_pos_multiplier: float,
    margin_first_capacity_enabled: bool,
    margin_load: float,
    margin_first_normal_margin_ratio: float,
    margin_first_max_margin_ratio: float,
    margin_first_reduced_size_ratio: float,
) -> float:
    """有效基础仓位 USDT（原 _effective_base_pos_usdt）。

    纯计算版本：所有依赖以参数传入，不读写策略状态。
    """
    base = base_pos_usdt
    if side in {"long", "short"}:
        base = side_float(side_config, side, "base_position_usdt", base)
    if in_recovery:
        base *= max(0.25, min(1.0, recovery_pos_multiplier))
    if (
        bool(margin_first_capacity_enabled)
        and margin_load >= safe_float(margin_first_normal_margin_ratio)
        and margin_load < safe_float(margin_first_max_margin_ratio)
    ):
        base *= safe_float(margin_first_reduced_size_ratio)
    return max(1.0, base)


def effective_max_side_notional_usdt(limits: Dict, side: str = "") -> float:
    """动态名义上限按边取值（原 _effective_max_side_notional_usdt）。"""
    return limits.get(f"{side}_cap", limits["side_cap"])


def daily_drawdown_ratio(day_equity_baseline: float, cached_equity: float) -> float:
    """当日回撤比例（原 _daily_drawdown_ratio）。"""
    if day_equity_baseline <= 0 or cached_equity <= 0:
        return 0.0
    return max(0.0, (day_equity_baseline - cached_equity) / day_equity_baseline)
