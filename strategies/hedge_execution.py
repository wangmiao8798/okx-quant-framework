# -*- coding: utf-8 -*-
"""执行适配函数集合，从 FuturesHedgeStrategy 提取（拆分第五阶段）。

设计约束（拆分契约）：
- 订单结果解析（order_error_code / extract_order_id / order_succeeded）为纯函数，
  显式传入结果 dict，输出与旧方法完全一致。
- 执行方法（submit_order / place_open_order / cancel_order / set_leverage）为
  编排级适配器：保持策略实例签名（self 作为显式受控适配器），函数体与旧实现
  逐字一致；所有副作用（gateway 下单/撤单/设杠杆、预算 allocator 预留/提交/
  释放、日志、风控回调）仍经 self 上的受控接口执行。
- 本模块不 import strategies.futures_hedge，避免循环依赖。
"""
from __future__ import annotations

from execution.gateway import ExecutionGateway
from utils.helpers import safe_float


# ---------------------------------------------------------------------------
# 订单结果解析：纯函数
# ---------------------------------------------------------------------------

def order_error_code(result) -> str:
    """提取订单结果的第一个错误码（原 _order_error_code）。"""
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    for key in ("sCode", "code", "errCode"):
                        val = item.get(key)
                        if val not in (None, "", "0"):
                            return str(val)
        for key in ("sCode", "code", "errCode"):
            val = result.get(key)
            if val not in (None, "", "0"):
                return str(val)
    return ""


def extract_order_id(result: dict) -> str:
    """提取订单 id（原 _extract_order_id）。"""
    if not isinstance(result, dict):
        return ""
    oid = str(result.get("ordId", "") or "").strip()
    if oid:
        return oid
    data = result.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                oid = str(item.get("ordId", "") or "").strip()
                if oid:
                    return oid
    elif isinstance(data, dict):
        oid = str(data.get("ordId", "") or "").strip()
        if oid:
            return oid
    return ""


def order_succeeded(result: dict) -> bool:
    """订单是否成功（原 _order_succeeded）。"""
    if not isinstance(result, dict):
        return False
    code = str(result.get("sCode", result.get("code", "")) or "").strip()
    if code and code not in {"0", "00000"}:
        return False
    return bool(extract_order_id(result))


# ---------------------------------------------------------------------------
# 执行适配器：self 为显式受控适配器，函数体与旧实现逐字一致
# ---------------------------------------------------------------------------

def submit_order(
    self,
    symbol: str,
    side: str,
    qty: float,
    ord_type: str = "market",
    px=None,
    pos_side: str = "",
    reduce_only: bool = False,
    tgt_ccy: str = "",
    pending_orders=None,
    paired_net_neutral: bool = False,
    inventory_repair: bool = False,
    allow_hard_total_cap: bool = False,
):
    gateway = getattr(self, "_execution_gateway", None)
    if gateway is None and self._client:
        gateway = ExecutionGateway(self._client)
        self._execution_gateway = gateway
    if gateway is None:
        return {}
    if not reduce_only:
        order_px = safe_float(px) or safe_float(getattr(self, "_last_price", 0.0))
        reason = self._opening_order_block_reason(
            str(pos_side or "").lower(),
            qty,
            order_px,
            pending_orders=pending_orders,
            paired_net_neutral=paired_net_neutral,
            inventory_repair=inventory_repair,
            allow_hard_total_cap=allow_hard_total_cap,
        )
        if reason:
            self._log_opening_guard_block(pos_side, reason, qty, order_px)
            return {"sCode": "opening_guard", "sMsg": reason, "guard_blocked": True}
    budget_token = ""
    reserved_margin = 0.0
    if not reduce_only and getattr(self, "_account_budget_allocator", None) is not None:
        order_px = safe_float(px) or safe_float(getattr(self, "_last_price", 0.0))
        reserved_margin = self._order_margin_required(qty, order_px)
        budget_token = self._account_budget_allocator.reserve(
            reserved_margin,
            symbol or self._symbol,
            "paired" if str(pos_side or "") else "open",
        )
        if not budget_token:
            return {
                "sCode": "account_margin_budget",
                "sMsg": "account_margin_budget",
                "guard_blocked": True,
            }
    risk_context = self._risk_context_snapshot()
    if budget_token:
        # The gateway evaluates the account budget after reservation. Carry
        # this order's reservation so RiskPolicy does not double-charge it.
        risk_context["account"]["budget_order_reservation_margin"] = reserved_margin
    kwargs = {
        "ord_type": ord_type,
        "px": str(px) if px is not None else None,
        "pos_side": pos_side,
        "reduce_only": reduce_only,
        "risk_context": risk_context,
    }
    if tgt_ccy:
        kwargs["tgt_ccy"] = tgt_ccy
    if paired_net_neutral:
        kwargs["paired_net_neutral"] = True
    if inventory_repair:
        # This explicit marker lets RiskGate distinguish a bounded
        # net-reducing repair from an ordinary margin-increasing add.
        kwargs["inventory_repair"] = True
    if reduce_only:
        kwargs["intent_type"] = "reduce_only"
    elif inventory_repair:
        kwargs["intent_type"] = "inventory_repair"
    elif paired_net_neutral:
        kwargs["intent_type"] = "paired_net_neutral"
    else:
        kwargs["intent_type"] = "normal_grid"
    result = gateway.place_order(symbol or self._symbol, side, str(qty), **kwargs)
    if reduce_only and order_error_code(result) == "51169":
        invalidate = getattr(self, "_invalidate_tp_side", None)
        if callable(invalidate) and str(pos_side or "").lower() in {"long", "short"}:
            invalidate(str(pos_side).lower(), reason="exchange_51169")
        if isinstance(result, dict):
            result["no_position_reject"] = True
    if budget_token:
        if order_succeeded(result):
            self._account_budget_allocator.commit(budget_token)
        else:
            self._account_budget_allocator.release(budget_token)
    return result


def place_open_order(
    self,
    side: str,
    qty: float,
    pos_side: str,
    ord_type: str = "market",
    px=None,
    pending_orders=None,
    paired_net_neutral: bool = False,
    inventory_repair: bool = False,
    allow_hard_total_cap: bool = False,
):
    return submit_order(
        self,
        self._symbol,
        side,
        str(qty),
        ord_type=ord_type,
        px=str(px) if px is not None else None,
        pos_side=pos_side,
        pending_orders=pending_orders,
        paired_net_neutral=paired_net_neutral,
        inventory_repair=inventory_repair,
        allow_hard_total_cap=allow_hard_total_cap,
    )


def cancel_order(self, ord_id: str, inst_id: str = "") -> bool:
    gateway = getattr(self, "_execution_gateway", None)
    if gateway is None and self._client:
        gateway = ExecutionGateway(self._client)
        self._execution_gateway = gateway
    if gateway is None or not ord_id:
        return False
    try:
        return gateway.cancel_order(inst_id or self._symbol, ord_id)
    except Exception as exc:
        self.log.warning(f"[cancel] {inst_id or self._symbol} ordId={ord_id}: {exc}")
        return False


def set_leverage(self, symbol: str, lever: int, margin_mode: str = ""):
    gateway = getattr(self, "_execution_gateway", None)
    if gateway is None and self._client:
        gateway = ExecutionGateway(self._client)
        self._execution_gateway = gateway
    if gateway is None:
        return
    try:
        result = gateway.set_leverage(
            symbol or self._symbol,
            lever,
            margin_mode or self._margin_mode,
        )
        if not isinstance(result, dict) or str(result.get("code", "0")) != "0":
            raise RuntimeError(f"set leverage failed: {result}")
        return result
    except Exception as exc:
        self.log.warning(f"[lev] {symbol or self._symbol} {lever}x: {exc}")
        raise
