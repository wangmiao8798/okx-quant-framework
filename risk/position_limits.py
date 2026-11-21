"""Pure position and exposure limit calculations."""


def net_exposure_side_blocks(long_qty: float, short_qty: float, max_net_exposure: float) -> tuple[bool, bool]:
    net = float(long_qty or 0.0) - float(short_qty or 0.0)
    limit = max(0.0, float(max_net_exposure or 0.0))
    return net > limit, -net > limit


def net_exposure_ratio(long_notional: float, short_notional: float) -> float:
    """Return absolute net exposure as a fraction of gross exposure."""
    long_notional = max(0.0, float(long_notional or 0.0))
    short_notional = max(0.0, float(short_notional or 0.0))
    gross = long_notional + short_notional
    if gross <= 0.0:
        return 0.0
    return min(1.0, abs(long_notional - short_notional) / gross)


def net_aware_margin_stop_ratio(
    base_stop_ratio: float,
    net_ratio: float,
    max_stop_ratio: float,
    hedge_credit_ratio: float = 0.20,
) -> float:
    """Raise a soft margin stop only for an already-established hedge.

    The credit is proportional to the offsetting side and is capped by the
    configured account risk ceiling. A one-sided position (net_ratio=1) gets
    no credit; a balanced hedge gets at most ``hedge_credit_ratio``.
    """
    base = max(0.0, min(1.0, float(base_stop_ratio or 0.0)))
    maximum = max(base, min(1.0, float(max_stop_ratio or base)))
    ratio = max(0.0, min(1.0, float(net_ratio or 0.0)))
    credit = max(0.0, min(maximum - base, float(hedge_credit_ratio or 0.0)))
    return min(maximum, base + credit * (1.0 - ratio))


def opening_limits_block_reason(
    side: str,
    long_before: float,
    short_before: float,
    quantity: float,
    contract_value: float,
    price: float,
    side_cap: float,
    net_cap: float,
    margin_load: float = 1.0,
    inventory_repair_side_cap_multiplier: float = 1.0,
    inventory_repair_max_margin_ratio: float = 0.35,
    total_cap: float = 0.0,
    allow_net_reducing_total_cap: bool = False,
    inventory_repair: bool = False,
) -> str:
    if side not in {"long", "short"}:
        return "invalid_side"
    quantity = abs(float(quantity or 0.0))
    contract_value = float(contract_value or 0.0)
    price = float(price or 0.0)
    if quantity <= 0 or contract_value <= 0 or price <= 0:
        return "invalid_order_size"

    long_before = float(long_before or 0.0)
    short_before = float(short_before or 0.0)
    long_after = long_before + (quantity if side == "long" else 0.0)
    short_after = short_before + (quantity if side == "short" else 0.0)
    side_after = long_after if side == "long" else short_after
    net_before = long_before - short_before
    net_after = long_after - short_after
    side_notional_after = side_after * contract_value * price
    total_notional_after = (long_after + short_after) * contract_value * price

    side_cap = max(0.0, float(side_cap or 0.0))
    if side_cap > 0 and side_notional_after > side_cap:
        repair_multiplier = max(1.0, float(inventory_repair_side_cap_multiplier or 1.0))
        net_reducing = abs(net_after) + 1e-12 < abs(net_before)
        within_repair_cap = side_notional_after <= side_cap * repair_multiplier
        # Only an explicitly classified inventory repair may use the bounded
        # side-cap overage. A normal grid order must never inherit this
        # exception merely because its net delta happens to be favorable.
        if not (inventory_repair and net_reducing and within_repair_cap):
            return "max_side_notional"

    total_cap = max(0.0, float(total_cap or 0.0))
    if total_cap > 0 and total_notional_after > total_cap:
        # A weak-side opening increases gross notional but reduces directional
        # exposure. ``total_cap`` is the working quote cap, so it must not
        # prevent inventory repair; account budget, side cap, margin and the
        # exchange remain authoritative safety boundaries.
        net_reducing = abs(net_after) + 1e-12 < abs(net_before)
        if not (allow_net_reducing_total_cap and inventory_repair and net_reducing):
            return "max_total_notional"

    net_cap = max(0.0, float(net_cap or 0.0))
    if net_cap > 0 and abs(net_after) * contract_value * price > net_cap:
        if abs(net_after) > abs(net_before) + 1e-12:
            return "max_net_notional"
    return ""


def dynamic_notional_limits(
    base_side_cap: float,
    equity: float,
    margin_load_ratio: float,
    mgn_ratio=None,
    long_notional: float = 0.0,
    short_notional: float = 0.0,
    enabled: bool = False,
    max_multiplier: float = 1.4,
    mgn_ratio_floor: float = 1.0,
    mgn_ratio_full: float = 5.0,
    available_margin: float = 0.0,
    leverage: float = 1.0,
    equity_ratio: float = 0.0,
    margin_budget_utilization: float = 0.80,
    base_long_cap: float | None = None,
    base_short_cap: float | None = None,
    base_total_cap: float | None = None,
) -> dict:
    base_side_cap = max(0.0, float(base_side_cap or 0.0))
    long_base = base_side_cap if base_long_cap is None else max(0.0, float(base_long_cap or 0.0))
    short_base = base_side_cap if base_short_cap is None else max(0.0, float(base_short_cap or 0.0))
    base_total = (
        max(0.0, float(base_total_cap or 0.0))
        if base_total_cap is not None
        else long_base + short_base
    )
    equity = max(0.0, float(equity or 0.0))
    if (long_base <= 0 and short_base <= 0) or not enabled or equity <= 0:
        return {
            "side_cap": min(long_base, short_base),
            "long_cap": long_base,
            "short_cap": short_base,
            "total_cap": base_total,
            "scale": 1.0,
            "net_ratio": 0.0,
            "margin_budget_usdt": 0.0,
            "margin_based_total_cap": 0.0,
        }

    margin_load = max(0.0, float(margin_load_ratio or 0.0))
    ratio = float(mgn_ratio or 0.0)
    if ratio > 0:
        health = (ratio - mgn_ratio_floor) / max(0.001, mgn_ratio_full - mgn_ratio_floor)
    else:
        health = 0.25 if margin_load <= 0 else 1.0 - margin_load / 0.65
    health = max(0.0, min(1.0, health))
    max_multiplier = max(1.0, float(max_multiplier or 1.0))
    scale = 1.0 + (max_multiplier - 1.0) * health
    scaled_total_cap = base_total * scale
    total_cap = scaled_total_cap
    scaled_long_cap = long_base * scale
    scaled_short_cap = short_base * scale

    # The fixed per-symbol cap is the hard ceiling.  When explicitly enabled,
    # available account margin can reduce that ceiling dynamically, but never
    # increase it by itself.  The configured multiplier remains the deliberate
    # hard-cap expansion knob, while the margin budget prevents stale balance
    # snapshots from authorizing more notional than the account can fund.
    margin_budget_usdt = 0.0
    margin_based_total_cap = 0.0
    if equity_ratio > 0 and leverage > 0 and available_margin > 0:
        allocation = min(available_margin, equity * equity_ratio)
        margin_budget_usdt = max(0.0, allocation)
        margin_based_total_cap = margin_budget_usdt * leverage * max(
            0.0, min(1.0, margin_budget_utilization)
        )
        if margin_based_total_cap > 0:
            total_cap = min(total_cap, margin_based_total_cap)
    gross = max(0.0, float(long_notional or 0.0)) + max(0.0, float(short_notional or 0.0))
    net_ratio = net_exposure_ratio(long_notional, short_notional)
    scaled_gross_cap = scaled_long_cap + scaled_short_cap
    if scaled_gross_cap > 0 and total_cap < scaled_gross_cap:
        total_share = total_cap / scaled_gross_cap
        long_cap = scaled_long_cap * total_share
        short_cap = scaled_short_cap * total_share
    else:
        long_cap = scaled_long_cap
        short_cap = scaled_short_cap
    dominant = "long" if long_notional > short_notional else "short" if short_notional > long_notional else ""
    if net_ratio > 0.25 and dominant:
        adjusted_cap = (total_cap / 2.0) * max(0.5, 1.0 - (net_ratio - 0.25) * 0.5)
        if dominant == "long":
            long_cap = adjusted_cap
        else:
            short_cap = adjusted_cap
    return {
        "side_cap": min(long_cap, short_cap),
        "long_cap": long_cap,
        "short_cap": short_cap,
        "total_cap": total_cap,
        "scale": scale,
        "scaled_total_cap": scaled_total_cap,
        "net_ratio": net_ratio,
        "margin_health": health,
        "dominant_side": dominant,
        "margin_budget_usdt": margin_budget_usdt,
        "margin_based_total_cap": margin_based_total_cap,
    }
