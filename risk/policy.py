"""Pure order-risk decisions shared by the execution boundary."""

import time

from utils.helpers import safe_float
from risk.account_budget import order_margin_requirement


ORDER_INTENTS = frozenset(
    {
        "normal_grid",
        "paired_net_neutral",
        "inventory_repair",
        "reduce_only",
    }
)


INVENTORY_REPAIR_HARD_OVERRIDE_SYMBOLS = frozenset(
    {"CRV-USDT-SWAP", "FARTCOIN-USDT-SWAP", "DRAM-USDT-SWAP", "RAM-USDT-SWAP"}
)


def normalize_order_intent(payload=None, reduce_only: bool = False) -> tuple[str, str]:
    """Return one canonical order intent and reject contradictory markers."""
    payload = payload if isinstance(payload, dict) else {}
    explicit = str(payload.get("intent_type") or "").strip().lower()
    if explicit == "missing_side_repair":
        explicit = "inventory_repair"
    if explicit and explicit not in ORDER_INTENTS:
        return "", "unknown_order_intent"

    flagged = []
    if bool(payload.get("inventory_repair", False)):
        flagged.append("inventory_repair")
    if bool(payload.get("paired_net_neutral", False)):
        flagged.append("paired_net_neutral")
    if reduce_only:
        if explicit and explicit != "reduce_only":
            return "", "intent_conflict"
        if flagged:
            return "", "intent_conflict"
        return "reduce_only", ""
    if len(set(flagged)) > 1:
        return "", "intent_conflict"
    flagged_intent = flagged[0] if flagged else ""
    if explicit and flagged_intent and explicit != flagged_intent:
        return "", "intent_conflict"
    return explicit or flagged_intent or "normal_grid", ""


def validate_order_context(context, symbol: str, now: float | None = None) -> str:
    """Validate the per-symbol snapshot required for an opening decision."""
    if not isinstance(context, dict):
        return "risk_context_unavailable"
    context_symbol = str(context.get("symbol") or "").strip().upper()
    expected_symbol = str(symbol or "").strip().upper()
    if not context_symbol or not expected_symbol or context_symbol != expected_symbol:
        return "risk_context_symbol_mismatch"
    generated_at = safe_float(context.get("generated_at"))
    if generated_at <= 0:
        return "risk_context_timestamp_missing"
    account = context.get("account")
    positions = context.get("positions")
    account_risk = context.get("account_risk")
    if not isinstance(account, dict) or not isinstance(positions, dict):
        return "risk_context_unavailable"
    if not isinstance(account_risk, dict) or not isinstance(account_risk.get("usdt"), dict):
        return "account_risk_unavailable"
    required_account = (
        "margin_load_ratio",
        "account_margin_soft_limit",
        "account_margin_hard_limit",
        "account_margin_resume_limit",
        "mgn_ratio_open_limit",
    )
    if any(key not in account for key in required_account):
        return "risk_context_account_fields_missing"
    # Strategy-generated contexts include equity and the configured minimum.
    # Keep older external/test callers compatible when those optional fields
    # are absent; an explicitly supplied invalid value still fails closed.
    if "equity_usdt" in account:
        equity = safe_float(account.get("equity_usdt"))
        if equity <= 0.0:
            return "account_equity_unavailable"
        minimum_equity = max(0.0, safe_float(account.get("min_open_equity_usdt", 0.0)))
        if minimum_equity > 0.0 and equity < minimum_equity:
            return "account_equity_below_minimum"
    max_age = safe_float(account.get("risk_context_max_age_sec", 30.0))
    max_age = max(5.0, min(300.0, max_age or 30.0))
    clock = time.time() if now is None else float(now)
    if clock - generated_at > max_age:
        return "risk_context_stale"
    observed_at_ms = safe_float(account_risk.get("observed_at_ms"))
    if observed_at_ms > 0 and clock - observed_at_ms / 1000.0 > max_age:
        return "account_risk_stale"
    return ""


def account_margin_mode(margin_load: float, soft_limit: float, hard_limit: float) -> str:
    """Classify margin: soft is throttling, hard is an opening veto."""
    load = max(0.0, safe_float(margin_load))
    soft = max(0.0, safe_float(soft_limit))
    hard = max(soft, safe_float(hard_limit))
    if hard > 0.0 and load >= hard:
        return "HARD"
    if soft > 0.0 and load >= soft:
        return "THROTTLED"
    return "ACTIVE"


def inventory_repair_hard_margin_override(
    context, side: str, pos_side: str, payload=None
) -> dict:
    """Evaluate the sole opt-in corridor that may cross the account hard line.

    This is intentionally a pure, fail-closed function so the strategy's
    early sizing gate and the final RiskGate decision use exactly the same
    eligibility rules. It does not replace any other opening guard.
    """
    context = context if isinstance(context, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    account = context.get("account") if isinstance(context.get("account"), dict) else {}
    positions = context.get("positions") if isinstance(context.get("positions"), dict) else {}
    account_risk = (
        context.get("account_risk")
        if isinstance(context.get("account_risk"), dict)
        else {}
    )
    usdt = account_risk.get("usdt") if isinstance(account_risk.get("usdt"), dict) else {}
    result = {
        "allowed": False,
        "reason": "disabled",
        "symbol": str(context.get("symbol") or "").strip().upper(),
        "override_symbol": str(
            account.get("inventory_repair_hard_override_symbol") or ""
        ).strip().upper(),
        "override_limit": safe_float(
            account.get("inventory_repair_hard_override_margin_ratio")
        ),
        "mgn_ratio_min": safe_float(
            account.get("inventory_repair_hard_override_mgn_ratio_min")
        ),
        "max_step_notional_usdt": safe_float(
            account.get("inventory_repair_hard_override_max_step_notional_usdt")
        ),
        "order_notional_usdt": 0.0,
        "required_margin": 0.0,
        "projected_margin_load": 0.0,
        "net_before": 0.0,
        "net_after": 0.0,
    }

    if not bool(account.get("inventory_repair_hard_override_enabled", False)):
        return result
    if result["override_symbol"] not in INVENTORY_REPAIR_HARD_OVERRIDE_SYMBOLS:
        result["reason"] = "override_symbol_not_allowed"
        return result
    if result["symbol"] != result["override_symbol"]:
        result["reason"] = "symbol_not_allowed"
        return result

    intent, intent_error = normalize_order_intent(payload, False)
    if intent_error or intent != "inventory_repair":
        result["reason"] = "inventory_repair_intent_required"
        return result
    if str(side or "").lower() != "buy" or str(pos_side or "").lower() != "long":
        result["reason"] = "repair_direction_not_allowed"
        return result

    quantity = abs(safe_float(payload.get("quantity")))
    long_before = max(0.0, safe_float(positions.get("long_qty")))
    short_before = max(0.0, safe_float(positions.get("short_qty")))
    net_before = long_before - short_before
    net_after = net_before + quantity
    result["net_before"] = net_before
    result["net_after"] = net_after
    # This is a short-inventory repair, not a generic weak-side exception.
    # Do not let a repair cross through flat and create a new net-long bias.
    if (
        quantity <= 0.0
        or net_before >= 0.0
        or quantity > abs(net_before) + 1e-12
        or abs(net_after) + 1e-12 >= abs(net_before)
    ):
        result["reason"] = "repair_must_reduce_net_short"
        return result

    mgn_ratio = safe_float(usdt.get("mgnRatio"))
    if result["mgn_ratio_min"] <= 0.0 or mgn_ratio < result["mgn_ratio_min"]:
        result["reason"] = "mgn_ratio_below_override_min"
        return result

    price = safe_float(payload.get("px")) or safe_float(account.get("mark_price"))
    contract_value = safe_float(account.get("contract_value"))
    leverage = max(1.0, safe_float(account.get("leverage", 1.0)))
    if quantity <= 0.0 or price <= 0.0 or contract_value <= 0.0:
        result["reason"] = "invalid_order_size"
        return result
    order_notional = quantity * price * contract_value
    result["order_notional_usdt"] = order_notional
    if (
        result["max_step_notional_usdt"] <= 0.0
        or order_notional > result["max_step_notional_usdt"] + 1e-9
    ):
        result["reason"] = "override_step_notional_limit"
        return result

    equity = safe_float(account.get("equity_usdt"))
    if equity <= 0.0:
        result["reason"] = "account_equity_unavailable"
        return result
    if "frozen_margin_usdt" in account:
        frozen_margin = max(0.0, safe_float(account.get("frozen_margin_usdt")))
    else:
        frozen_margin = max(0.0, safe_float(account.get("margin_load_ratio"))) * equity
    required_margin = order_margin_requirement(
        order_notional,
        leverage,
        safe_float(account.get("margin_buffer_ratio", 0.10)),
    )
    projected_margin_load = (frozen_margin + required_margin) / equity
    result["required_margin"] = required_margin
    result["projected_margin_load"] = projected_margin_load

    hard_limit = safe_float(account.get("account_margin_hard_limit"))
    if result["override_limit"] <= hard_limit:
        result["reason"] = "override_limit_not_above_hard_limit"
        return result
    if projected_margin_load >= result["override_limit"] - 1e-12:
        result["reason"] = "projected_margin_limit"
        return result

    if bool(account.get("account_budget_enabled", False)):
        available_budget = max(0.0, safe_float(account.get("budget_remaining_margin")))
        available_budget += max(
            0.0, safe_float(account.get("budget_order_reservation_margin"))
        )
        if required_margin > available_budget + 1e-9:
            result["reason"] = "account_margin_budget"
            return result

    result.update(allowed=True, reason="allowed")
    return result


class RiskPolicy:
    # One API/account is shared by all strategy instances.  This latch keeps a
    # brief dip below the hard line from immediately reopening risk; it clears
    # only after the account returns below the configured recovery line.
    _hard_margin_latches = {}

    @staticmethod
    def shadow_order(context, side: str, pos_side: str, reduce_only: bool, payload=None) -> dict:
        context = context if isinstance(context, dict) else {}
        payload = payload or {}
        shadow = {"mode": "reduce_only" if reduce_only else "open", "reason_codes": []}
        intent, intent_error = normalize_order_intent(payload, reduce_only)
        shadow["intent_type"] = intent
        if intent_error:
            shadow["reason_codes"].append("order_intent_invalid")
        account = context.get("account") if isinstance(context.get("account"), dict) else {}
        margin_load = safe_float(account.get("margin_load_ratio"))
        margin_limit = safe_float(
            account.get("account_margin_soft_limit", account.get("max_margin_ratio"))
        )
        paired_net_neutral = intent == "paired_net_neutral"
        paired_margin_limit = safe_float(
            account.get("paired_net_neutral_max_margin_ratio")
        )
        hard_margin_limit = safe_float(
            account.get("account_margin_hard_limit", account.get("hard_margin_ratio"))
        )
        resume_margin_limit = safe_float(account.get("account_margin_resume_limit", 0.50))
        scope = str(account.get("account_margin_scope") or "shared_okx_account")
        latched = bool(RiskPolicy._hard_margin_latches.get(scope, False))
        if hard_margin_limit > 0:
            if margin_load >= hard_margin_limit:
                latched = True
            elif latched and resume_margin_limit > 0 and margin_load < resume_margin_limit:
                latched = False
            RiskPolicy._hard_margin_latches[scope] = latched
        # 提前计算净敞口变化，判断加仓方向是否"降低净敞口"（对冲腿方向）。
        # 降低净敞口的加仓在跨仓下只占维持保证金、反而降冻结率，故在显式
        # opt-in(net_reducing_hard_margin_exempt_enabled)时不拦硬线；
        # 扩大净敞口的加仓仍照拦，保证金安全底线不变。
        positions = context.get("positions") if isinstance(context.get("positions"), dict) else {}
        long_before = safe_float(positions.get("long_qty"))
        short_before = safe_float(positions.get("short_qty"))
        max_net = safe_float(positions.get("max_net_exposure"))
        max_net_notional = safe_float(positions.get("max_net_notional_usdt"))
        quantity = abs(safe_float(payload.get("quantity")))
        side = str(side or "").lower()
        pos_side = str(pos_side or "").lower()
        delta = 0.0
        if pos_side == "long":
            delta = quantity if side == "buy" else -quantity if side == "sell" else 0.0
        elif pos_side == "short":
            delta = -quantity if side == "sell" else quantity if side == "buy" else 0.0
        net_before = long_before - short_before
        net_after = net_before + delta
        net_reducing = abs(net_after) + 1e-12 < abs(net_before)
        net_reducing_hard_exempt = bool(
            net_reducing
            and account.get("net_reducing_hard_margin_exempt_enabled", False)
        )
        hard_margin_blocked = bool(
            not reduce_only
            and hard_margin_limit > 0
            and (margin_load >= hard_margin_limit or latched)
            and not net_reducing_hard_exempt
        )
        if hard_margin_blocked:
            shadow["reason_codes"].append("account_hard_margin_limit")
        raw = context.get("account_risk") if isinstance(context.get("account_risk"), dict) else {}
        usdt = raw.get("usdt") if isinstance(raw.get("usdt"), dict) else {}
        mgn_ratio = safe_float(usdt.get("mgnRatio"))
        mgn_open_limit = safe_float(account.get("mgn_ratio_open_limit", 5.0))
        if not reduce_only and mgn_ratio > 0 and mgn_open_limit > 0 and mgn_ratio < mgn_open_limit:
            shadow["reason_codes"].append("mgn_ratio_limit")
        inventory_repair = intent == "inventory_repair"
        net_reducing_repair = inventory_repair and abs(net_after) + 1e-12 < abs(net_before)
        repair_hard_override = inventory_repair_hard_margin_override(
            context, side, pos_side, payload
        )
        hard_override_applied = bool(
            hard_margin_blocked and repair_hard_override.get("allowed")
        )
        if hard_override_applied:
            shadow["reason_codes"] = [
                reason
                for reason in shadow["reason_codes"]
                if reason != "account_hard_margin_limit"
            ]
        paired_margin_override = (
            paired_net_neutral
            and paired_margin_limit > 0
            and margin_load < paired_margin_limit
        )
        if net_reducing_repair or paired_margin_override:
            # Repair may cross the soft operating target, but never the hard
            # account line or its recovery latch. A paired order gets the same
            # exception only within its explicit per-profile margin cap.
            shadow["reason_codes"] = [r for r in shadow["reason_codes"] if r != "account_margin_limit"]
        order_px = safe_float(payload.get("px")) or safe_float(account.get("mark_price"))
        contract_value = max(0.0, safe_float(account.get("contract_value", 1.0)))
        if not reduce_only and not paired_net_neutral and max_net_notional > 0 and order_px > 0 and contract_value > 0:
            net_before_notional = abs(net_before) * order_px * contract_value
            net_after_notional = abs(net_after) * order_px * contract_value
            if (
                net_after_notional > max_net_notional
                and net_after_notional > net_before_notional + 1e-12
            ):
                shadow["reason_codes"].append("net_notional_limit")
        elif not reduce_only and max_net > 0 and abs(net_after) > max_net and abs(net_after) > abs(net_before) + 1e-12:
            # Backward compatibility for non-strategy callers that still
            # provide the historical unit-based limit.
            shadow["reason_codes"].append("net_exposure_limit")
        budget_enabled = bool(account.get("account_budget_enabled", False))
        budget_remaining = safe_float(account.get("budget_remaining_margin"))
        # The strategy reserves this order before the final gateway check. The
        # remaining budget therefore excludes this exact reservation. Add back
        # only that reservation; other symbols' reservations stay unavailable.
        budget_order_reservation = max(
            0.0, safe_float(account.get("budget_order_reservation_margin"))
        )
        budget_available_for_order = budget_remaining + budget_order_reservation
        required_margin = 0.0
        if not reduce_only:
            leverage = max(1.0, safe_float(account.get("leverage", 1.0)))
            required_margin = order_margin_requirement(
                quantity * order_px * contract_value,
                leverage,
                safe_float(account.get("margin_buffer_ratio", 0.10)),
            )
        if not reduce_only and budget_enabled:
            if required_margin > budget_available_for_order + 1e-9:
                shadow["reason_codes"].append("account_margin_budget")
        shadow["metrics"] = {
            "net_before": net_before,
            "net_after": net_after,
            "max_net_exposure": max_net,
            "max_net_notional_usdt": max_net_notional,
            "margin_load_ratio": margin_load,
            "max_margin_ratio": margin_limit,
            "account_margin_soft_limit": margin_limit,
            "account_margin_hard_limit": hard_margin_limit,
            "account_margin_resume_limit": resume_margin_limit,
            "account_margin_hard_latched": latched,
            "account_margin_mode": account_margin_mode(
                margin_load, margin_limit, hard_margin_limit
            ),
            "account_margin_soft_exceeded": bool(
                margin_limit > 0 and margin_load >= margin_limit
            ),
            "paired_net_neutral_max_margin_ratio": paired_margin_limit,
            "paired_margin_override": paired_margin_override,
            "mgnRatio": usdt.get("mgnRatio"),
            "mmr": usdt.get("mmr"),
            "budget_remaining_margin": budget_remaining,
            "budget_order_reservation_margin": budget_order_reservation,
            "budget_available_for_order_margin": budget_available_for_order,
            "required_margin": required_margin,
            "inventory_repair_hard_override_eligible": bool(
                repair_hard_override.get("allowed")
            ),
            "inventory_repair_hard_override_applied": hard_override_applied,
            "inventory_repair_hard_override_reason": repair_hard_override.get("reason", ""),
            "inventory_repair_projected_margin_load": safe_float(
                repair_hard_override.get("projected_margin_load")
            ),
            "inventory_repair_override_limit": safe_float(
                repair_hard_override.get("override_limit")
            ),
        }
        shadow["would_block"] = bool(shadow["reason_codes"])
        return shadow

    @staticmethod
    def evaluate_order(
        state: str,
        symbol: str,
        side: str,
        pos_side: str,
        reduce_only: bool,
        payload=None,
        reconciler_status=None,
        positions=None,
        max_reconciler_age: float = 90.0,
        now: float | None = None,
        context=None,
    ) -> dict:
        state = str(state or "")
        payload = payload or {}
        decision = {
            "state": state,
            "allowed": False,
            "reason_code": "",
            "symbol": str(symbol or ""),
            "side": str(side or ""),
            "pos_side": str(pos_side or ""),
            "reduce_only": bool(reduce_only),
            "payload": payload,
        }
        decision["shadow"] = RiskPolicy.shadow_order(context, side, pos_side, reduce_only, payload)
        intent, intent_error = normalize_order_intent(payload, reduce_only)
        decision["intent_type"] = intent
        if intent_error:
            decision["reason_code"] = "order_intent_invalid"
            return decision
        if not reduce_only:
            context_reason = validate_order_context(context, symbol)
            if context_reason:
                decision["shadow"]["reason_codes"].append(context_reason)
                decision["shadow"]["would_block"] = True
                decision["reason_code"] = context_reason
                return decision
        if reduce_only:
            decision.update(allowed=True, reason_code="risk_reduction_allowed")
            return decision
        # Prefer the strongest blocking reason over the order in which shadow
        # checks happened. This keeps a true hard-line breach visible even
        # though it also exceeds the soft operating line.
        reason_priority = (
            "account_hard_margin_limit",
            "mgn_ratio_limit",
            "account_margin_budget",
            "net_notional_limit",
            "net_exposure_limit",
        )
        reason_codes = set(decision["shadow"].get("reason_codes", []))
        for reason in reason_priority:
            if reason in reason_codes:
                decision["reason_code"] = reason
                return decision
        if state == "INVENTORY_REPAIR":
            if intent != "inventory_repair":
                decision["reason_code"] = "inventory_repair_intent_required"
                return decision
            positions = positions or []
            quantities = {
                str(row.get("pos_side")): abs(safe_float(row.get("quantity")))
                for row in positions
            }
            net_before = quantities.get("long", 0.0) - quantities.get("short", 0.0)
            quantity = abs(safe_float(payload.get("quantity", 0.0)))
            side = str(side or "").lower()
            pos_side = str(pos_side or "").lower()
            delta = 0.0
            if pos_side == "long":
                delta = quantity if side == "buy" else -quantity if side == "sell" else 0.0
            elif pos_side == "short":
                delta = -quantity if side == "sell" else quantity if side == "buy" else 0.0
            net_after = net_before + delta
            if quantity > 0 and abs(net_after) + 1e-12 < abs(net_before):
                decision.update(allowed=True, reason_code="inventory_repair_allowed")
                return decision
            decision["reason_code"] = "inventory_repair_worsens_net_exposure"
            return decision
        if state != "ACTIVE":
            decision["reason_code"] = f"risk_state_{state.lower()}"
            return decision
        reconciler_status = reconciler_status or {}
        if not reconciler_status.get("ready"):
            decision["reason_code"] = "reconciler_not_ready"
            return decision
        last_success = float(reconciler_status.get("last_success") or 0.0)
        clock = time.time() if now is None else float(now)
        if last_success <= 0 or clock - last_success > max(30.0, float(max_reconciler_age)):
            decision["reason_code"] = "reconciler_stale"
            return decision
        if decision["shadow"]["metrics"].get(
            "inventory_repair_hard_override_applied", False
        ):
            decision.update(
                allowed=True,
                reason_code="inventory_repair_hard_margin_override_allowed",
            )
            return decision
        decision.update(allowed=True, reason_code="active")
        return decision

    @staticmethod
    def evaluate_mutation(operation: str, symbol: str = "", payload=None, state: str = "") -> dict:
        state = str(state or "")
        operation = str(operation or "")
        if operation == "set_leverage" and state not in {"", "ACTIVE"}:
            return {
                "state": state,
                "allowed": False,
                "reason_code": f"risk_state_{state.lower()}",
                "symbol": str(symbol or ""),
                "side": "",
                "pos_side": "",
                "reduce_only": False,
                "payload": {"operation": operation, **(payload or {})},
            }
        return {
            "state": state,
            "allowed": True,
            "reason_code": "mutation_allowed" if operation == "set_leverage" else "risk_reduction_allowed",
            "symbol": str(symbol or ""),
            "side": "",
            "pos_side": "",
            "reduce_only": operation != "set_leverage",
            "payload": {"operation": operation, **(payload or {})},
        }
