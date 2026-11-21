from decimal import Decimal, ROUND_HALF_UP

SYMBOL = "HUMA-USDT-SWAP"


def _round_tick(value: float, tick_size: float) -> float:
    value_d = Decimal(str(value))
    tick_d = Decimal(str(tick_size))
    steps = (value_d / tick_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(steps * tick_d)


def build_plan(
    price: float,
    ct_val: float,
    tick_size: float,
    levels: int = 3,
    grid_step: float = 0.008,
    tp_ratio: float = 0.012,
) -> dict:
    if price <= 0 or ct_val <= 0 or tick_size <= 0:
        raise ValueError("price, ct_val and tick_size must be positive")
    levels = max(1, min(int(levels), 3))
    long_entries = []
    short_entries = []
    for level in range(1, levels + 1):
        long_entry = _round_tick(price * (1 - grid_step * level), tick_size)
        short_entry = _round_tick(price * (1 + grid_step * level), tick_size)
        long_entries.append({
            "level": level,
            "contracts": 1,
            "entry": long_entry,
            "tp": _round_tick(long_entry * (1 + tp_ratio), tick_size),
            "reduce_only_tp": True,
        })
        short_entries.append({
            "level": level,
            "contracts": 1,
            "entry": short_entry,
            "tp": _round_tick(short_entry * (1 - tp_ratio), tick_size),
            "reduce_only_tp": True,
        })
    contract_notional = round(price * ct_val, 8)
    return {
        "symbol": SYMBOL,
        "live_enabled": False,
        "mode": "observation",
        "contracts_per_level": 1,
        "contract_notional_usdt": contract_notional,
        "long_entries": long_entries,
        "short_entries": short_entries,
        "max_side_notional_usdt": round(contract_notional * levels, 8),
        "loss_rebalance_enabled": False,
        "notes": [
            "standalone ctVal-aware two-sided plan",
            "not loaded by the live multi-profile engine",
            "requires rebuilt host and rotated OKX API key before activation",
        ],
    }
