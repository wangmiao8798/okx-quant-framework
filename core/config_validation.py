"""Read-only validation for the main and multi-symbol strategy profiles.

The validator deliberately does not normalize or rewrite configuration.  A
bad safety boundary is a startup error; a likely deployment drift is exposed
as a warning for the control plane and operator logs.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _profile_label(config: dict, path: str) -> str:
    profile = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    return str(profile.get("runtime_name") or profile.get("slug") or os.path.basename(path) or "profile")


def _validate_profile(config: Any, path: str, errors: list[str], warnings: list[str]) -> dict:
    label = _profile_label(config if isinstance(config, dict) else {}, path)
    if not isinstance(config, dict):
        errors.append(f"{label}: YAML root must be a mapping")
        return {"label": label, "path": path, "symbol": ""}

    global_cfg = config.get("global") if isinstance(config.get("global"), dict) else {}
    exchange_cfg = config.get("exchange") if isinstance(config.get("exchange"), dict) else {}
    okx_cfg = exchange_cfg.get("okx") if isinstance(exchange_cfg.get("okx"), dict) else {}
    demo_mode = bool(global_cfg.get("demo", False))
    okx_flag = str(okx_cfg.get("flag", "")).strip()
    if okx_flag not in {"0", "1"}:
        errors.append(f"{label}: exchange.okx.flag must be '0' or '1'")
    elif demo_mode and okx_flag != "1":
        errors.append(
            f"{label}: global.demo=true requires exchange.okx.flag='1' (OKX demo environment)"
        )
    elif not demo_mode and okx_flag != "0":
        errors.append(
            f"{label}: global.demo=false requires exchange.okx.flag='0' (OKX live environment)"
        )

    futures = config.get("futures") if isinstance(config.get("futures"), dict) else {}
    deprecated = {
        "max_net_exposure",
        "rebalance_trigger_ratio",
        "rebalance_profit_ratio",
        "rebalance_cooldown_sec",
        "rebalance_max_chunk_pct",
        "rebalance_reduce_ratio",
    }
    for key in sorted(deprecated.intersection(futures)):
        warnings.append(f"{label}: deprecated futures.{key} is ignored")
    symbol = str(futures.get("symbol") or "").strip().upper()
    if not symbol:
        errors.append(f"{label}: futures.symbol is empty")

    leverage = _number(futures.get("leverage"), 0.0)
    if leverage < 1.0:
        errors.append(f"{label}: futures.leverage must be >= 1")
    elif leverage > 100.0:
        warnings.append(f"{label}: futures.leverage={leverage:g} is unusually high")

    open_limit = _number(futures.get("mgn_ratio_open_limit"), 5.0)
    pause_limit = _number(futures.get("mgn_ratio_pause_limit"), 3.0)
    emergency_limit = _number(futures.get("mgn_ratio_emergency_limit"), 2.0)
    if min(open_limit, pause_limit, emergency_limit) <= 0:
        errors.append(f"{label}: mgn_ratio limits must be positive")
    elif not (open_limit >= pause_limit >= emergency_limit):
        errors.append(
            f"{label}: mgn_ratio limits must satisfy open >= pause >= emergency"
        )

    legacy_side_cap_present = "max_side_notional_usdt" in futures
    side_cap = _number(futures.get("max_side_notional_usdt"), 0.0)
    long_cap = _number(futures.get("max_long_side_notional_usdt"), side_cap)
    short_cap = _number(futures.get("max_short_side_notional_usdt"), side_cap)
    total_cap = _number(futures.get("max_total_notional_usdt"), 0.0)
    net_cap = _number(futures.get("max_net_notional_usdt"), 0.0)
    if long_cap <= 0 or short_cap <= 0:
        errors.append(f"{label}: futures long/short notional caps must be > 0")
    if legacy_side_cap_present:
        warnings.append(f"{label}: futures.max_side_notional_usdt is legacy; use explicit long/short caps")
    if net_cap <= 0:
        errors.append(f"{label}: futures.max_net_notional_usdt must be > 0")
    if total_cap > 0 and net_cap > total_cap:
        errors.append(f"{label}: max_net_notional_usdt must not exceed max_total_notional_usdt")
    if long_cap > 0 and short_cap > 0 and net_cap > long_cap + short_cap:
        warnings.append(f"{label}: net notional cap exceeds two-sided cap")
    cooldown = _number(futures.get("add_cooldown"), -1.0)
    cooldown_sec = _number(futures.get("add_cooldown_sec"), -1.0)
    if cooldown < 0 and cooldown_sec < 0:
        warnings.append(f"{label}: no add cooldown configured; strategy defaults apply")
    elif cooldown >= 0 and cooldown_sec >= 0 and cooldown != cooldown_sec:
        warnings.append(f"{label}: add_cooldown is global cooldown; add_cooldown_sec is side cooldown")

    for key, low, high in (
        ("dynamic_side_notional_equity_ratio", 0.0, 1.0),
        ("dynamic_side_notional_margin_budget_ratio", 0.0, 1.0),
        ("dynamic_side_notional_max_margin_ratio", 0.0, 1.0),
        ("max_margin_ratio", 0.0, 1.0),
        ("account_margin_buffer_ratio", 0.0, 0.5),
    ):
        if key in futures:
            value = _number(futures.get(key), -1.0)
            if not low <= value <= high:
                errors.append(f"{label}: futures.{key} must be in [{low}, {high}]")

    # These fields control the only exception to the account soft margin line.
    # They must be explicit in every live profile; silently falling back to a
    # constructor default is how a repair plan can disagree with its margin gate.
    required_risk_fields = (
        "grid_bias_ratio",
        "active_inventory_repair_enabled",
        "inventory_repair_imbalance_gate",
        "inventory_repair_side_cap_multiplier",
        "inventory_repair_max_margin_ratio",
        "inventory_repair_missing_side_margin_ratio",
        "inventory_repair_missing_side_resume_ratio",
        "account_margin_soft_limit",
        "account_margin_hard_limit",
        "account_margin_resume_limit",
        "mgn_ratio_open_limit",
        "mgn_ratio_pause_limit",
        "mgn_ratio_emergency_limit",
        "account_risk_max_age_sec",
        "max_margin_ratio",
        "hard_stop_enabled",
        "max_loss_usdt",
        "max_drawdown",
        "daily_loss_limit_usdt",
        "daily_drawdown",
        "coverage_safety_tp_enabled",
        "coverage_safety_tp_repair_limit",
        "reduce_only_mode",
        "paired_add_enabled",
        "paired_average_down_enabled",
        "paired_add_allow_inventory_pause",
        "paired_add_max_margin_ratio",
    )
    for key in required_risk_fields:
        if key not in futures:
            errors.append(f"{label}: futures.{key} must be explicit")
    for key in (
        "grid_bias_ratio",
        "inventory_repair_imbalance_gate",
        "inventory_repair_max_margin_ratio",
        "inventory_repair_missing_side_margin_ratio",
        "inventory_repair_missing_side_resume_ratio",
        "account_margin_soft_limit",
        "account_margin_hard_limit",
        "account_margin_resume_limit",
    ):
        if key in futures:
            value = _number(futures.get(key), -1.0)
            if not 0.0 <= value < 1.0:
                errors.append(f"{label}: futures.{key} must be in [0, 1)")

    repair_side_multiplier = _number(
        futures.get("inventory_repair_side_cap_multiplier"), -1.0
    )
    if repair_side_multiplier < 1.0 or repair_side_multiplier > 2.0:
        errors.append(
            f"{label}: futures.inventory_repair_side_cap_multiplier must be in [1, 2]"
        )
    risk_age = _number(futures.get("account_risk_max_age_sec"), -1.0)
    if risk_age < 5.0 or risk_age > 300.0:
        errors.append(
            f"{label}: futures.account_risk_max_age_sec must be in [5, 300]"
        )

    soft_margin = _number(futures.get("account_margin_soft_limit"), -1.0)
    hard_margin = _number(futures.get("account_margin_hard_limit"), -1.0)
    resume_margin = _number(futures.get("account_margin_resume_limit"), -1.0)
    repair_margin = _number(futures.get("inventory_repair_max_margin_ratio"), -1.0)
    missing_margin = _number(
        futures.get("inventory_repair_missing_side_margin_ratio"), -1.0
    )
    missing_resume = _number(
        futures.get("inventory_repair_missing_side_resume_ratio"), -1.0
    )
    if soft_margin >= 0 and hard_margin >= 0 and soft_margin >= hard_margin:
        errors.append(f"{label}: account_margin_soft_limit must not exceed hard_limit")
    if resume_margin >= 0 and hard_margin >= 0 and resume_margin >= hard_margin:
        errors.append(f"{label}: account_margin_resume_limit must not exceed hard_limit")
    if repair_margin >= 0 and hard_margin >= 0 and repair_margin >= hard_margin:
        errors.append(f"{label}: inventory_repair_max_margin_ratio must be below hard_limit")
    if missing_margin >= 0 and hard_margin >= 0 and missing_margin > hard_margin:
        errors.append(f"{label}: inventory_repair_missing_side_margin_ratio must not exceed hard_limit")
    if missing_resume >= 0 and missing_margin >= 0 and missing_resume > missing_margin:
        errors.append(
            f"{label}: inventory_repair_missing_side_resume_ratio must not exceed missing_side_margin_ratio"
        )

    # --- Added 2026-10-16: enforce single-profile margin ceiling vs shared hard line ---
    # A profile-level max_margin_ratio above the shared account hard limit means a
    # single strategy can by itself approach (or breach) the account-wide guard.
    # This was previously unchecked, allowing e.g. 0.55 against a 0.30 hard line.
    profile_margin = _number(
        futures.get("max_margin_ratio", futures.get("margin_first_max_margin_ratio")), -1.0
    )
    if profile_margin >= 0 and hard_margin >= 0 and profile_margin >= hard_margin:
        errors.append(
            f"{label}: max_margin_ratio must be strictly below account_margin_hard_limit"
            f" (got {profile_margin}, hard_limit {hard_margin})"
        )
    # Leverage ceiling: unbounded leverage on a high-volatility altcoin profile is
    # the main amplifier of tail losses. Cap at 5x unless explicitly justified.
    leverage = _number(futures.get("leverage"), -1.0)
    # Ceiling stays 5 by default. A profile may opt in to a higher value only
    # through an explicit leverage_ceiling field, and never beyond an absolute
    # hard cap of 10 -- this keeps the global guard intact while allowing a
    # profile whose exchange leverage was deliberately raised to stay in sync.
    leverage_ceiling = _number(futures.get("leverage_ceiling"), 5.0)
    leverage_ceiling = max(1.0, min(leverage_ceiling, 10.0))
    if leverage >= 0 and leverage > leverage_ceiling:
        errors.append(
            f"{label}: futures.leverage must not exceed {leverage_ceiling:g} (got {leverage})"
        )

    # The account hard line is shared by all live profiles. The only permitted
    # exception is a bounded CRV net-short repair corridor, whose direction is
    # enforced in RiskPolicy. Keeping this schema narrow prevents a later
    # profile copy from silently granting ordinary grids the same exception.
    hard_override_enabled = bool(
        futures.get("inventory_repair_hard_override_enabled", False)
    )
    if hard_override_enabled:
        required_override_fields = (
            "inventory_repair_hard_override_symbol",
            "inventory_repair_hard_override_margin_ratio",
            "inventory_repair_hard_override_mgn_ratio_min",
            "inventory_repair_hard_override_max_step_notional_usdt",
        )
        for key in required_override_fields:
            if key not in futures:
                errors.append(f"{label}: futures.{key} must be explicit when override is enabled")
        override_symbol = str(
            futures.get("inventory_repair_hard_override_symbol") or ""
        ).strip().upper()
        allowed_override_symbols = {"CRV-USDT-SWAP", "FARTCOIN-USDT-SWAP", "DRAM-USDT-SWAP"}
        if symbol not in allowed_override_symbols or override_symbol != symbol:
            errors.append(
                f"{label}: inventory_repair hard override is restricted to CRV-USDT-SWAP, FARTCOIN-USDT-SWAP, or DRAM-USDT-SWAP and must match the profile symbol"
            )
        override_margin = _number(
            futures.get("inventory_repair_hard_override_margin_ratio"), -1.0
        )
        if hard_margin >= 0 and not hard_margin < override_margin <= 0.75:
            errors.append(
                f"{label}: inventory_repair_hard_override_margin_ratio must be above hard_limit and at most 0.75"
            )
        override_mgn_min = _number(
            futures.get("inventory_repair_hard_override_mgn_ratio_min"), -1.0
        )
        if override_mgn_min < open_limit:
            errors.append(
                f"{label}: inventory_repair_hard_override_mgn_ratio_min must be at least mgn_ratio_open_limit"
            )
        override_step_notional = _number(
            futures.get("inventory_repair_hard_override_max_step_notional_usdt"), -1.0
        )
        max_bounded_step = min(25.0, max(0.0, net_cap) * 0.10)
        if override_step_notional <= 0.0 or override_step_notional > max_bounded_step:
            errors.append(
                f"{label}: inventory_repair_hard_override_max_step_notional_usdt must be in (0, {max_bounded_step:g}]"
            )

    for side in ("long", "short"):
        side_cfg = futures.get(side)
        if not isinstance(side_cfg, dict):
            errors.append(f"{label}: futures.{side} must be a mapping")
            continue
        for key in (
            "enabled",
            "base_position_usdt",
            "add_step",
            "add_multiplier",
            "max_add_layers",
            "first_tp_ratio",
            "final_tp_ratio",
            "trailing_enabled",
            "trailing_tp_retreat",
            "entry_trigger_price",
            "entry_trigger_condition",
        ):
            if key not in side_cfg:
                errors.append(f"{label}: futures.{side}.{key} must be explicit")

    multiplier = _number(futures.get("dynamic_side_notional_max_multiplier"), 1.0)
    if multiplier < 1.0:
        errors.append(f"{label}: dynamic_side_notional_max_multiplier must be >= 1")

    profile = config.get("profile") if isinstance(config.get("profile"), dict) else {}
    profile_symbol = str(profile.get("symbol") or "").strip().upper()
    if profile_symbol and symbol and profile_symbol != symbol:
        errors.append(f"{label}: profile.symbol={profile_symbol} differs from futures.symbol={symbol}")
    runtime_name = str(profile.get("runtime_name") or "").strip()
    if runtime_name and symbol:
        slug = symbol.split("-")[0].lower()
        expected = "futures" if label == "base" else f"futures_{slug}"
        if runtime_name != expected:
            warnings.append(f"{label}: profile.runtime_name={runtime_name} expected {expected}")

    budget = config.get("risk", {}).get("account_budget", {}) if isinstance(config.get("risk"), dict) else {}
    if not isinstance(budget, dict):
        budget = {}
    for key, low, high in (
        ("target_margin_ratio", 0.0, 1.0),
        ("reserve_equity_ratio", 0.0, 0.5),
        ("utilization", 0.0, 1.0),
    ):
        if key in budget:
            value = _number(budget.get(key), -1.0)
            if not low <= value <= high:
                errors.append(f"{label}: risk.account_budget.{key} must be in [{low}, {high}]")

    return {"label": label, "path": path, "symbol": symbol, "runtime_name": runtime_name}


def validate_config_bundle(main_config: Any, main_path: str, extra_configs: Iterable[dict] = ()) -> dict:
    """Validate loaded configs and return a JSON-safe status payload."""
    errors: list[str] = []
    warnings: list[str] = []
    profiles = [_validate_profile(main_config, str(main_path), errors, warnings)]
    for item in extra_configs or ():
        if isinstance(item, tuple) and len(item) == 2:
            path, config = item
        else:
            path, config = "<extra>", item
        profiles.append(_validate_profile(config, str(path), errors, warnings))

    seen: dict[str, str] = {}
    for profile in profiles:
        symbol = profile.get("symbol") or ""
        if not symbol:
            continue
        previous = seen.get(symbol)
        if previous:
            errors.append(f"duplicate futures.symbol={symbol}: {previous} and {profile['path']}")
        else:
            seen[symbol] = str(profile["path"])

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "profiles": profiles,
        "checked_at": time.time(),
    }
