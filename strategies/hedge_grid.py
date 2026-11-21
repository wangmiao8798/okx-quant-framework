# -*- coding: utf-8 -*-
"""网格与库存编排的只读辅助函数，从 FuturesHedgeStrategy 提取（拆分第四阶段）。

设计约束（拆分契约）：
- 无副作用：不写策略状态、不下单、不触发持久化。
- 显式入参：策略状态与受控回调全部由调用方（原方法代理）注入。
- 输出与旧方法完全一致。
- 本模块不 import strategies.futures_hedge，避免循环依赖。
"""
from __future__ import annotations

import math
from typing import Callable, Optional

from utils.helpers import safe_float


def position_qty_tolerance(qty_step: float) -> float:
    """持仓数量容差（原 _position_qty_tolerance）。"""
    return max(1e-12, safe_float(qty_step) * 0.5)


def pending_open_qty(
    side: str,
    fetch_live_orders: Callable[[], list],
    log_warning: Callable[[str], None],
) -> Optional[float]:
    """返回某侧在途非 reduce-only 数量；状态不可用时返回 None。

    原 _pending_open_qty：被动挂单不计入在途恢复单。
    """
    if side not in {"long", "short"}:
        return None
    try:
        total = 0.0
        for order in fetch_live_orders():
            if str(order.get("reduceOnly", "false")).lower() == "true":
                continue
            if str(order.get("posSide") or "").lower() != side:
                continue
            order_type = str(order.get("ordType") or order.get("order_type") or "").lower()
            if order_type in {"limit", "post_only", "optimal_limit_ioc"}:
                continue
            total += abs(safe_float(order.get("sz", 0)))
        return total
    except Exception as exc:
        log_warning(f"[missing_side_repair] live order state unavailable side={side}: {exc}")
        return None


def pending_repair_qty(
    side: str,
    price: float,
    effective_side_step: Callable[[str], float],
    fetch_live_orders: Callable[[], list],
) -> float:
    """同价位附近（约两个步长内）的在途修复数量（原 _pending_repair_qty）。"""
    expected_order_side = "buy" if side == "long" else "sell"
    max_distance = max(effective_side_step(side) * 2.0, 0.01)
    total = 0.0
    for order in fetch_live_orders():
        if str(order.get("reduceOnly", "false")).lower() == "true":
            continue
        if str(order.get("posSide") or "").lower() != side:
            continue
        if str(order.get("side") or "").lower() != expected_order_side:
            continue
        order_price = safe_float(order.get("px", 0))
        if order_price <= 0 or abs(order_price - price) / max(price, 1e-8) > max_distance:
            continue
        total += abs(safe_float(order.get("sz", 0)))
    return total


def inventory_repair_step_qty_cap(
    repair_side: str,
    price: float,
    desired_qty: float,
    hard_override_enabled: bool,
    max_step_notional_usdt: float,
    ct_val: float,
    qty_step: float,
) -> float:
    """CRV 修复走廊的步进数量上限（原 _inventory_repair_step_qty_cap）。"""
    desired_qty = max(0.0, safe_float(desired_qty))
    if not hard_override_enabled:
        return desired_qty
    if repair_side != "long" or price <= 0.0:
        return desired_qty
    max_notional = safe_float(max_step_notional_usdt)
    contract_value = safe_float(ct_val)
    step = max(0.0, safe_float(qty_step))
    if max_notional <= 0.0 or contract_value <= 0.0 or step <= 0.0:
        return 0.0
    max_qty = max_notional / (price * contract_value)
    max_qty = math.floor((max_qty + 1e-12) / step) * step
    return min(desired_qty, max(0.0, max_qty))


def inventory_repair_margin_blocked(
    active_inventory_repair_enabled: bool,
    account_margin_data_block_reason: Callable[[], str],
    mgn_ratio_opening_block_reason: Callable[[], str],
    margin_load: float,
    margin_limit: float,
    hard_override_decision: Callable[[str, float, float], dict],
    repair_side: str = "",
    price: float = 0.0,
    quantity: float = 0.0,
) -> bool:
    """修复专用保证金闸门（原 _inventory_repair_margin_blocked）。"""
    if not active_inventory_repair_enabled:
        return True
    if account_margin_data_block_reason():
        return True
    if mgn_ratio_opening_block_reason():
        return True
    if margin_limit > 0.0 and margin_load < margin_limit:
        return False
    override = hard_override_decision(repair_side, price, quantity)
    return not bool(override.get("allowed"))


def preserve_opening_grids_during_inventory_repair(
    symbol: str,
    hedged_grid_allow_dominant_refill: bool,
    margin_first_capacity_ready: Callable[[], bool],
) -> bool:
    """同频带弱侧修复期间是否保留 DRAM maker 网格（原 _preserve_opening_grids_during_inventory_repair）。"""
    return (
        str(symbol).upper().startswith("DRAM-")
        and bool(hedged_grid_allow_dominant_refill)
        and margin_first_capacity_ready()
    )
