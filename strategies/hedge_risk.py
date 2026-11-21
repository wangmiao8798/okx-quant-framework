# -*- coding: utf-8 -*-
"""风控决策纯函数集合，从 FuturesHedgeStrategy 提取（拆分第三阶段）。

设计约束（拆分契约）：
- 无副作用：不写策略状态、不下单、不触发持久化；带写入语义的方法（如
  _inventory_repair_plan 的 update_mode）由原方法代理保留写入。
- 大决策方法以不可变 RiskContext 数据类输入；小方法用显式参数。
- 输出与旧方法完全一致（reason 字符串 / float / dict / tuple）。
- 本模块不 import strategies.futures_hedge，避免循环依赖。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from utils.helpers import safe_float
from risk.account_budget import order_margin_requirement
from risk.position_limits import (
    net_exposure_ratio,
    net_aware_margin_stop_ratio,
    opening_limits_block_reason,
)


# ---------------------------------------------------------------------------
# 小决策：显式参数
# ---------------------------------------------------------------------------

def order_margin_required(
    qty: float, price: float, ct_val: float,
    cur_lev: float, margin_buffer_ratio: float,
) -> float:
    """单笔订单所需保证金（原 _order_margin_required）。"""
    notional = abs(safe_float(qty)) * max(0.0, safe_float(price)) * max(0.0, safe_float(ct_val))
    return order_margin_requirement(
        notional,
        max(1.0, safe_float(cur_lev)),
        safe_float(margin_buffer_ratio),
    )


def account_budget_block_reason(
    allocator_remaining_margin: Optional[float],
    required_margin: float,
    pending_margin: float,
) -> str:
    """共享账户预算阻断原因（原 _account_budget_block_reason）。"""
    if allocator_remaining_margin is None:
        return ""
    required = required_margin + max(0.0, safe_float(pending_margin))
    if required > allocator_remaining_margin + 1e-9:
        return "account_margin_budget"
    return ""


def mgn_ratio_opening_block_reason(
    account_margin_data_block_reason: str,
    mgn_ratio: float,
    mgn_ratio_open_limit: float,
    mgn_ratio_emergency_limit: float,
    mgn_ratio_pause_limit: float,
) -> str:
    """mgnRatio 开仓阻断原因（原 _mgn_ratio_opening_block_reason）。"""
    if account_margin_data_block_reason:
        return account_margin_data_block_reason
    ratio = mgn_ratio
    limit = max(0.0, safe_float(mgn_ratio_open_limit))
    if ratio > 0 and limit > 0 and ratio < limit:
        if ratio < safe_float(mgn_ratio_emergency_limit):
            return "mgn_ratio_emergency"
        if ratio < safe_float(mgn_ratio_pause_limit):
            return "mgn_ratio_pause"
        return "mgn_ratio_limit"
    return ""


def inventory_margin_thresholds(
    long_qty: float,
    short_qty: float,
    inventory_margin_stop_ratio: float,
    max_margin_ratio: float,
    account_margin_soft_limit: float,
    inventory_margin_hedge_credit_ratio: float,
    inventory_margin_resume_ratio: float,
) -> Tuple[float, float, float]:
    """库存修复保证金阈值 (stop, resume, hedge_ratio)（原 _inventory_margin_thresholds）。"""
    long_qty = max(0.0, safe_float(long_qty))
    short_qty = max(0.0, safe_float(short_qty))
    gross_qty = long_qty + short_qty
    if gross_qty > 0.0 and long_qty > 0.0 and short_qty > 0.0:
        hedge_ratio = net_exposure_ratio(long_qty, short_qty)
        base_stop = safe_float(inventory_margin_stop_ratio)
        max_stop = max(
            base_stop,
            safe_float(max_margin_ratio),
            safe_float(account_margin_soft_limit),
        )
        stop_ratio = net_aware_margin_stop_ratio(
            base_stop,
            hedge_ratio,
            max_stop,
            safe_float(inventory_margin_hedge_credit_ratio),
        )
        resume_base = safe_float(inventory_margin_resume_ratio)
        resume_ratio = min(stop_ratio, resume_base + max(0.0, stop_ratio - base_stop))
    else:
        hedge_ratio = 1.0
        stop_ratio = safe_float(inventory_margin_stop_ratio)
        resume_ratio = safe_float(inventory_margin_resume_ratio)
    return stop_ratio, resume_ratio, hedge_ratio


def inventory_repair_plan(
    inventory,
    price: float,
    ct_val: float,
    max_net_notional_usdt: float,
    inventory_repair_imbalance_gate: float,
    inventory_repair_stage_ratio: float,
    inventory_repair_mode: bool,
    inventory_repair_exit_net_notional_usdt: float,
    inventory_repair_exit_imbalance_ratio: float,
    min_qty: float,
) -> Dict:
    """库存修复计划（原 _inventory_repair_plan 的纯计算部分）。

    注意：update_mode 的写状态逻辑保留在原方法代理中，本函数不写状态。
    """
    exit_net_notional = safe_float(inventory_repair_exit_net_notional_usdt)
    exit_net_notional += max(0.0, safe_float(min_qty)) * price * ct_val
    return inventory.repair_plan(
        price,
        ct_val,
        max_net_notional_usdt,
        safe_float(inventory_repair_imbalance_gate),
        safe_float(inventory_repair_stage_ratio),
        bool(inventory_repair_mode),
        exit_net_notional,
        safe_float(inventory_repair_exit_imbalance_ratio),
    )


def inventory_repair_cooldown(
    net_notional: float,
    max_net_notional_usdt: float,
    fast_cooldown_sec: float,
    medium_cooldown_sec: float,
    cooldown_sec: float,
) -> float:
    """库存修复冷却秒数（原 _inventory_repair_cooldown）。"""
    net_cap = max(0.0, safe_float(max_net_notional_usdt))
    if net_cap > 0 and net_notional > net_cap * 2.0:
        return safe_float(fast_cooldown_sec)
    if net_cap > 0 and net_notional > net_cap * 1.3:
        return safe_float(medium_cooldown_sec)
    return safe_float(cooldown_sec)


# ---------------------------------------------------------------------------
# 大决策：不可变 RiskContext
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OpeningRiskContext:
    """_opening_order_block_reason 的全部输入（不可变）。"""
    side: str
    qty: float
    price: float
    pending_orders: Optional[dict]
    paired_net_neutral: bool
    inventory_repair: bool
    allow_hard_total_cap: bool
    # --- 注入的策略状态 ---
    reduce_only_mode: bool
    risk_opening_pause_active: bool
    risk_opening_pause_reason: str
    instrument_info_ready: bool
    has_leverage_mismatch: bool
    client_present: bool
    fetch_live_orders: Callable[[], list]
    tp_coverage_block_reason: Callable[[str], str]
    last_price: float
    ct_val: float
    account_budget_allocator_present: bool
    account_budget_block_reason: Callable[[float, float, float], str]
    pending_opening_margin: Callable[[Optional[list]], float]
    long_qty: float
    short_qty: float
    dynamic_notional_limits: Callable[[], dict]
    margin_first_capacity_ready: Callable[[], bool]
    account_margin_load_ratio: Callable[[], float]
    cached_equity: float
    working_total_notional_ratio: float
    max_net_notional_usdt: float
    inventory_repair_side_cap_multiplier: float
    inventory_repair_max_margin_ratio: float


def opening_order_block_reason(ctx: OpeningRiskContext) -> str:
    """开仓阻断原因（原 _opening_order_block_reason，输出不变）。"""
    if ctx.side not in {"long", "short"}:
        return "invalid_side"
    if ctx.reduce_only_mode:
        return "reduce_only_mode"
    if ctx.risk_opening_pause_active:
        return str(ctx.risk_opening_pause_reason or "") or "risk_opening_pause"
    if not ctx.instrument_info_ready:
        return "instrument_info_unavailable"
    if ctx.has_leverage_mismatch:
        return "leverage_mismatch"
    live_orders = None
    if ctx.pending_orders is None and ctx.client_present:
        try:
            live_orders = ctx.fetch_live_orders()
        except Exception:
            return "live_order_state_unavailable"
    tp_block = ctx.tp_coverage_block_reason(ctx.side)
    if tp_block and not ctx.inventory_repair:
        return tp_block
    qty = abs(safe_float(ctx.qty))
    px = safe_float(ctx.price) or safe_float(ctx.last_price)
    if qty <= 0 or px <= 0 or safe_float(ctx.ct_val) <= 0:
        return "invalid_order_size"
    pending = {"long": 0.0, "short": 0.0}
    if ctx.pending_orders is not None:
        for pos_side in pending:
            pending[pos_side] = max(0.0, safe_float(ctx.pending_orders.get(pos_side, 0.0)))
    elif live_orders is not None:
        for order in live_orders:
            if str(order.get("reduceOnly", "")).lower() == "true":
                continue
            pos_side = str(order.get("posSide") or "").lower()
            if pos_side in pending:
                pending[pos_side] += abs(safe_float(order.get("sz", 0)))
    budget_reason = ctx.account_budget_block_reason(
        qty,
        px,
        0.0 if ctx.account_budget_allocator_present
        else ctx.pending_opening_margin(live_orders) if live_orders is not None else 0.0,
    )
    if budget_reason:
        return budget_reason
    long_before = ctx.long_qty + pending["long"]
    short_before = ctx.short_qty + pending["short"]
    notional_limits = ctx.dynamic_notional_limits()
    effective_side_cap = notional_limits.get(f"{ctx.side}_cap", notional_limits["side_cap"])
    total_cap = notional_limits["total_cap"]
    margin_first = ctx.margin_first_capacity_ready()
    if margin_first:
        effective_side_cap = 0.0
        total_cap = 0.0
    if not ctx.allow_hard_total_cap:
        total_cap *= safe_float(ctx.working_total_notional_ratio)
    margin_load = ctx.account_margin_load_ratio() if ctx.cached_equity > 0 else 1.0
    return opening_limits_block_reason(
        ctx.side,
        long_before,
        short_before,
        qty,
        ctx.ct_val,
        px,
        0.0 if ctx.paired_net_neutral else effective_side_cap,
        0.0 if ctx.paired_net_neutral else ctx.max_net_notional_usdt,
        margin_load,
        max(
            1.10,
            safe_float(ctx.inventory_repair_side_cap_multiplier),
        ),
        safe_float(ctx.inventory_repair_max_margin_ratio),
        total_cap,
        allow_net_reducing_total_cap=bool(ctx.allow_hard_total_cap or ctx.inventory_repair),
        inventory_repair=ctx.inventory_repair,
    )


@dataclass(frozen=True)
class CapacityRiskContext:
    """_grid_capacity_qty 的全部输入（不可变）。"""
    side: str
    desired_qty: float
    price: float
    pending_orders: Optional[dict]
    inventory_repair: bool
    # --- 注入的策略状态 ---
    long_qty: float
    short_qty: float
    ct_val: float
    dynamic_notional_limits: Callable[[], dict]
    margin_first_capacity_ready: Callable[[], bool]
    working_total_notional_ratio: float
    max_net_notional_usdt: float
    inventory_repair_side_cap_multiplier: float
    account_budget_snapshot: Callable[[], dict]
    cur_lev: float
    margin_buffer_ratio: float
    qty_step: float
    min_qty: float
    opening_order_block_reason: Callable[[str, float, float, Optional[dict], bool], str]


def grid_capacity_qty(ctx: CapacityRiskContext) -> float:
    """网格下单可用容量（原 _grid_capacity_qty，输出不变）。"""
    if ctx.side not in {"long", "short"}:
        return 0.0
    desired = abs(safe_float(ctx.desired_qty))
    px = safe_float(ctx.price)
    ct_val = max(0.0, safe_float(ctx.ct_val))
    if desired <= 0.0 or px <= 0.0 or ct_val <= 0.0:
        return 0.0

    pending = {"long": 0.0, "short": 0.0}
    if isinstance(ctx.pending_orders, dict):
        for pos_side in pending:
            pending[pos_side] = max(0.0, safe_float(ctx.pending_orders.get(pos_side, 0.0)))
    long_before = max(0.0, safe_float(ctx.long_qty)) + pending["long"]
    short_before = max(0.0, safe_float(ctx.short_qty)) + pending["short"]
    net_before = long_before - short_before
    weak_side = (ctx.side == "short" and net_before > 0.0) or (ctx.side == "long" and net_before < 0.0)
    imbalance_qty = abs(net_before)

    limits = ctx.dynamic_notional_limits()
    margin_first = ctx.margin_first_capacity_ready()
    side_cap = max(0.0, safe_float(limits.get(f"{ctx.side}_cap", limits.get("side_cap", 0.0))))
    if weak_side and ctx.inventory_repair:
        side_cap *= max(
            1.10,
            safe_float(ctx.inventory_repair_side_cap_multiplier),
        )
    unit_notional = px * ct_val
    capacities = [desired]
    if side_cap > 0.0 and not margin_first:
        side_before = long_before if ctx.side == "long" else short_before
        normal_room = max(0.0, side_cap - side_before * unit_notional) / unit_notional
        if weak_side and normal_room <= 1e-12:
            repair_room = max(0.0, side_cap * max(
                1.10,
                safe_float(ctx.inventory_repair_side_cap_multiplier),
            ) - side_before * unit_notional) / unit_notional
            capacities.append(min(repair_room, imbalance_qty))
        else:
            capacities.append(normal_room)

    total_cap = max(0.0, safe_float(limits.get("total_cap", 0.0)))
    if total_cap > 0.0 and not margin_first:
        total_before = (long_before + short_before) * unit_notional
        total_cap *= max(0.0, safe_float(ctx.working_total_notional_ratio))
        total_room = max(0.0, total_cap - total_before) / unit_notional
        capacities.append(max(imbalance_qty, total_room) if weak_side and ctx.inventory_repair else total_room)

    net_cap = max(0.0, safe_float(ctx.max_net_notional_usdt))
    if net_cap > 0.0:
        net_before_abs = abs(net_before) * unit_notional
        net_room = max(0.0, net_cap - net_before_abs) / unit_notional
        capacities.append(imbalance_qty + net_room if weak_side else net_room)

    try:
        budget = ctx.account_budget_snapshot()
    except Exception:
        budget = {}
    if budget.get("enabled"):
        leverage = max(1.0, safe_float(ctx.cur_lev))
        buffer = max(0.0, min(0.50, safe_float(ctx.margin_buffer_ratio)))
        pending_margin = (pending["long"] + pending["short"]) * unit_notional / leverage * (1.0 + buffer)
        margin_room = max(0.0, safe_float(budget.get("remaining_margin")) - pending_margin)
        capacities.append(margin_room * leverage / (unit_notional * (1.0 + buffer)))

    max_qty = max(0.0, min(capacities))
    step = max(0.0, safe_float(ctx.qty_step))
    min_qty = max(0.0, safe_float(ctx.min_qty))
    if step > 0.0:
        max_qty = math_floor_step(max_qty, step)
    if max_qty > 0.0 and step > 0.0:
        checks = min(1000, int(max_qty / step) + 1)
        for _ in range(checks):
            final_reason = ctx.opening_order_block_reason(
                ctx.side,
                max_qty,
                px,
                pending,
                ctx.inventory_repair,
            )
            if not final_reason:
                break
            if final_reason not in {
                "max_side_notional",
                "max_total_notional",
                "max_net_notional",
                "account_margin_budget",
            }:
                max_qty = 0.0
                break
            max_qty = round(max_qty - step, 12)
            if max_qty + 1e-12 < min_qty:
                max_qty = 0.0
                break
    if max_qty + 1e-12 < min_qty:
        return 0.0
    return max_qty


def math_floor_step(value: float, step: float) -> float:
    """按步长向下取整（保持与原实现相同的 1e-12 容差）。"""
    import math
    return math.floor((value + 1e-12) / step) * step
