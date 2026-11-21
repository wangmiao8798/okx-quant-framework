"""OKX V5 API 客户端封装

基于 okx SDK 的 REST API 函数封装
支持行情查询、账户管理、下单交易等操作
"""

from decimal import Decimal, InvalidOperation
from typing import Dict, List, Any, Optional, Callable

from utils.logger import get_logger
from utils.helpers import safe_float
from utils.execution_guard import ExecutionGuard
import threading
import time
import uuid

class OKXClient:
    # --- Cache for shared account data (multi-strategy optimization) ---
    _balance_cache = 0.0
    _balance_cache_time = 0.0
    CACHE_TTL = 3  # seconds

    def get_cached_equity(self) -> float:
        now = time.time()
        if now - self._balance_cache_time < self.CACHE_TTL:
            return self._balance_cache
        try:
            eq = self.get_total_equity()
            self._balance_cache = eq
            self._balance_cache_time = now
            return eq
        except:
            return self._balance_cache

    def get_cached_balance(self, ccy="USDT") -> float:
        now = time.time()
        if now - self._balance_cache_time < self.CACHE_TTL:
            return self._balance_cache
        try:
            bal = self.get_balance(ccy)
            self._balance_cache = bal
            self._balance_cache_time = now
            return bal
        except:
            return self._balance_cache

    """OKX API 客户端封装"""

    def __init__(self, credentials: dict, flag: str = '0', event_ledger=None):
        self.log = get_logger("api.okx")
        self.flag = flag
        self._event_ledger = event_ledger
        self._credentials = dict(credentials or {})
        self._sdk_request_count = 0
        self._sdk_recycle_threshold = 15000
        self._sdk_recycle_count = 0

        from okx.Account import AccountAPI
        from okx.Trade import TradeAPI
        from okx.MarketData import MarketAPI
        from okx.PublicData import PublicAPI
        self._account = AccountAPI(
            credentials["api_key"], credentials["api_secret"],
            credentials["passphrase"], flag=flag
        )
        self._trade = TradeAPI(
            credentials["api_key"], credentials["api_secret"],
            credentials["passphrase"], flag=flag
        )
        self._market = MarketAPI(credentials["api_key"], credentials["api_secret"], credentials["passphrase"], flag=flag)
        self._public = PublicAPI(credentials["api_key"], credentials["api_secret"], credentials["passphrase"], flag=flag)

        self._lock = threading.Lock()
        # Rate limiter
        self._call_times = []
        self._retries = 3
        self._retry_delay = 1
        self._rate_limit_window = 2.0
        self._rate_limit_max_calls = 8
        self._rate_backoff_until = 0.0
        self._rate_limit_failures = 0
        self._api_failure_window = []
        self._api_failure_lock = threading.Lock()
        self._clock = time.monotonic
        self._sleep = time.sleep
        self._unknown_orders = {}
        self._unknown_orders_lock = threading.Lock()
        self._startup_unknown_order_ids = set()
        self._load_unresolved_orders()
        self._prune_unresolved_orders_at_startup()
        self._execution_guard = ExecutionGuard(self)

    def _sdk_api_role(self, func) -> str:
        owner = getattr(func, "__self__", None)
        if owner is getattr(self, "_account", None):
            return "account"
        if owner is getattr(self, "_trade", None):
            return "trade"
        if owner is getattr(self, "_market", None):
            return "market"
        if owner is getattr(self, "_public", None):
            return "public"
        return ""

    def _sdk_method(self, role: str, name: str):
        owner = getattr(self, f"_{role}", None) if role else None
        method = getattr(owner, name, None) if owner is not None else None
        return method if callable(method) else None

    def _recycle_sdk_clients(self) -> None:
        """Recreate SDK HTTP/2 clients after a long-lived connection degrades."""
        credentials = dict(getattr(self, "_credentials", {}) or {})
        if not credentials:
            return
        try:
            for client in (
                getattr(self, "_account", None),
                getattr(self, "_trade", None),
                getattr(self, "_market", None),
                getattr(self, "_public", None),
            ):
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            from okx.Account import AccountAPI
            from okx.Trade import TradeAPI
            from okx.MarketData import MarketAPI
            from okx.PublicData import PublicAPI
            args = (
                credentials["api_key"],
                credentials["api_secret"],
                credentials["passphrase"],
            )
            self._account = AccountAPI(*args, flag=self.flag)
            self._trade = TradeAPI(*args, flag=self.flag)
            self._market = MarketAPI(*args, flag=self.flag)
            self._public = PublicAPI(*args, flag=self.flag)
            self._sdk_request_count = 0
            self._sdk_recycle_count += 1
            self.log.warning(f"[API] recycled SDK clients count={self._sdk_recycle_count}")
        except Exception as exc:
            self.log.error(f"[API] SDK client recycle failed: {exc}")

    @staticmethod
    def _is_transport_failure(exc_or_message) -> bool:
        text = str(exc_or_message or "").lower()
        return any(
            marker in text
            for marker in (
                "connectionterminated",
                "connection reset",
                "all operations failed",
                "stream_id",
                "http2",
            )
        )

    # ---------- 行情查询 ----------

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        resp = self._call(self._market.get_ticker, instId=symbol)
        d = (resp.get("data") or [{}])[0]
        return {
            "symbol": symbol,
            "last": safe_float(d.get("last")),
            "bid": safe_float(d.get("bid")),
            "ask": safe_float(d.get("ask")),
            "high24h": safe_float(d.get("high24h")),
            "low24h": safe_float(d.get("low24h")),
            "vol24h": safe_float(d.get("vol24h")),
        }

    def get_candles(self, symbol: str, bar: str = "5m", limit: int = 200) -> List[Dict]:
        resp = self._call(
            self._market.get_candlesticks,
            instId=symbol, bar=bar, limit=str(limit)
        )
        candles = []
        for item in resp.get("data", []):
            candles.append({
                "ts": int(item[0]), "open": safe_float(item[1]),
                "high": safe_float(item[2]), "low": safe_float(item[3]),
                "close": safe_float(item[4]), "vol": safe_float(item[5]),
            })
        return sorted(candles, key=lambda candle: candle["ts"])

    def get_spot_info(self, symbol: str) -> Dict:
        resp = self._call(self._public.get_instruments, instType="SPOT", instId=symbol)
        d = (resp.get("data") or [{}])[0]
        return {
            "minSz": safe_float(d.get("minSz", 0.00001)),
            "lotSz": safe_float(d.get("lotSz", 0.00001)),
            "tickSz": safe_float(d.get("tickSz", 0.01)),
        }

    def get_futures_info(self, symbol: str) -> Dict:
        resp = self._call(self._public.get_instruments, instType="SWAP", instId=symbol)
        data = resp.get("data") or []
        if not data:
            return {"ok": False, "symbol": symbol, "msg": resp.get("msg", "instrument not found")}
        d = data[0]
        return {
            "ok": True,
            "symbol": symbol,
            "instId": d.get("instId", symbol),
            "minSz": safe_float(d.get("minSz", 0.001)),
            "lotSz": safe_float(d.get("lotSz", 0.001)),
            "tickSz": safe_float(d.get("tickSz", 0.1)),
            "ctVal": safe_float(d.get("ctVal", 0.1)),
            "ctValCcy": d.get("ctValCcy", ""),
            "settleCcy": d.get("settleCcy", ""),
            "state": d.get("state", ""),
        }

    def check_api_key_permissions(self) -> Dict[str, Any]:
        warnings = ["未通过试下单检测交易权限；请确认 API 已开启交易权限且未开启提币权限"]
        result = {"ok": False, "can_read_balance": False, "risk_level": "unknown", "warnings": warnings}
        try:
            resp = self.get_account_balance_raw()
            if str(resp.get("code", "")) != "0":
                result["msg"] = "API Key 读取余额失败"
                result["risk_level"] = "high"
                return result
            balance = 0.0
            for item in resp.get("data", []):
                for detail in item.get("details", []):
                    if detail.get("ccy") == "USDT":
                        balance = safe_float(detail.get("availBal", 0))
                        break
            result.update({"ok": True, "can_read_balance": True, "risk_level": "low", "balance_usdt": balance})
        except Exception as exc:
            result["msg"] = f"API Key 读取余额失败: {type(exc).__name__}"
            result["risk_level"] = "high"
        return result

    # ---------- 交易 ----------

    def get_balance(self, ccy: str = "USDT") -> float:
        resp = self.get_account_balance_raw()
        for item in resp.get("data", []):
            for detail in item.get("details", []):
                if detail.get("ccy") == ccy:
                    return safe_float(detail.get("availBal", 0))
        return 0.0

    def get_total_equity(self) -> float:
        """获取账户总权益（含现货+合约浮盈浮亏）"""
        try:
            resp = self.get_account_balance_raw()
            for item in resp.get("data", []):
                eq = item.get("totalEq", "0")
                if eq:
                    return float(eq)
            return 0.0
        except Exception as e:
            self.log.warning(f"获取总权益失败: {e}")
            return 0.0

    def get_account_balance_raw(self) -> Dict[str, Any]:
        """Return the unmodified OKX account balance response."""
        return self._call(self._account.get_account_balance)

    def get_positions(self, symbol: str = "") -> List[Dict]:
        params = {"instId": symbol} if symbol else {}
        resp = self._call(self._account.get_positions, **params)
        if (
            not isinstance(resp, dict)
            or str(resp.get("code", "0")) != "0"
            or not isinstance(resp.get("data"), list)
        ):
            raise RuntimeError(f"position query failed: {resp}")
        return resp.get("data") or []

    def get_position_risk(self, symbol: str) -> Dict[str, float]:
        positions = self.get_positions(symbol)
        risk = {"long_sz": 0.0, "short_sz": 0.0,
                "long_liq": 0.0, "short_liq": 0.0,
                "margin": 0.0, "notional": 0.0, "upl": 0.0,
                "mark_px": 0.0, "positions_available": False}
        for pos in positions:
            side = pos.get("posSide", "")
            sz = safe_float(pos.get("pos", 0))
            risk["notional"] += safe_float(pos.get("notionalUsd", 0))
            position_margin = safe_float(pos.get("margin", 0)) or safe_float(pos.get("imr", 0))
            risk["margin"] += position_margin
            risk["upl"] += safe_float(pos.get("upl", 0))
            mark_px = safe_float(pos.get("markPx", 0))
            if mark_px > 0:
                risk["mark_px"] = mark_px
            if side == "long":
                risk["long_sz"] = sz
                risk["long_liq"] = safe_float(pos.get("liqPx", 0))
                risk["long_lever"] = safe_float(pos.get("lever", 0))
                risk["long_imr"] = position_margin
            elif side == "short":
                risk["short_sz"] = sz
                risk["short_liq"] = safe_float(pos.get("liqPx", 0))
                risk["short_lever"] = safe_float(pos.get("lever", 0))
                risk["short_imr"] = position_margin
        risk["positions_available"] = True
        return risk

    # ---------- 交易 ----------

    @staticmethod
    def _format_order_size(value: Any) -> str:
        """Normalize binary-float artifacts before sending lot-sized quantities."""
        try:
            normalized = Decimal(str(value)).quantize(Decimal("0.00000001"))
            return format(normalized, "f").rstrip("0").rstrip(".") or "0"
        except (InvalidOperation, TypeError, ValueError):
            return str(value)

    def _create_order_intent(self, params: Dict[str, Any]) -> str:
        ledger = getattr(self, "_event_ledger", None)
        if ledger is None:
            return ""
        try:
            return ledger.create_order_intent(params)
        except Exception as exc:
            self.log.warning(f"[Ledger] intent record failed: {exc}")
            return ""

    def _record_order_event(self, intent_id: str, event_type: str, **kwargs):
        ledger = getattr(self, "_event_ledger", None)
        if ledger is None:
            return
        try:
            ledger.append_event(intent_id or None, event_type, **kwargs)
        except Exception as exc:
            self.log.warning(f"[Ledger] event record failed: {exc}")

    @staticmethod
    def _new_client_order_id() -> str:
        return "oq" + uuid.uuid4().hex[:30]

    def _load_unresolved_orders(self):
        ledger = getattr(self, "_event_ledger", None)
        if ledger is None:
            return
        try:
            unresolved = ledger.get_unresolved_order_intents()
        except Exception as exc:
            self.log.warning(f"[Ledger] unresolved intent load failed: {exc}")
            return
        for item in unresolved:
            cl_ord_id = str(item.get("cl_ord_id") or "")
            if cl_ord_id:
                self._remember_unknown(
                    cl_ord_id,
                    str(item.get("symbol") or ""),
                    str(item.get("intent_id") or ""),
                    persisted=True,
                )

    def _prune_unresolved_orders_at_startup(self):
        """Resolve stale unknown intents once per symbol before strategies start."""
        lock = getattr(self, "_unknown_orders_lock", None)
        if lock is None:
            return
        with lock:
            symbols = sorted({
                str(item.get("symbol") or "")
                for item in getattr(self, "_unknown_orders", {}).values()
                if item.get("symbol")
            })
        for symbol in symbols:
            try:
                self._bulk_prune_missing_unknown_orders(symbol)
            except Exception as exc:
                self.log.warning("[Order] startup unknown-order cleanup failed for %s: %s", symbol, exc)

    def _remember_unknown(
        self,
        cl_ord_id: str,
        symbol: str,
        intent_id: str,
        persisted: bool = True,
    ):
        lock = getattr(self, "_unknown_orders_lock", None)
        if lock is None:
            self._unknown_orders_lock = threading.Lock()
            lock = self._unknown_orders_lock
        with lock:
            unknown = getattr(self, "_unknown_orders", {})
            unknown[cl_ord_id] = {
                "cl_ord_id": cl_ord_id,
                "symbol": symbol,
                "intent_id": intent_id,
            }
            self._unknown_orders = unknown
            startup_ids = getattr(self, "_startup_unknown_order_ids", set())
            if persisted:
                startup_ids.add(cl_ord_id)
            else:
                startup_ids.discard(cl_ord_id)
            self._startup_unknown_order_ids = startup_ids

    def _forget_unknown(self, cl_ord_id: str):
        lock = getattr(self, "_unknown_orders_lock", None)
        if lock is None:
            return
        with lock:
            getattr(self, "_unknown_orders", {}).pop(cl_ord_id, None)
            getattr(self, "_startup_unknown_order_ids", set()).discard(cl_ord_id)

    @staticmethod
    def _is_order_not_found_response(response: Dict[str, Any]) -> bool:
        """Return true only when the exchange explicitly confirms no order exists."""
        if not isinstance(response, dict):
            return False
        code = str(response.get("code", response.get("sCode", "")) or "")
        message = " ".join(
            str(response.get(key, "") or "")
            for key in ("msg", "sMsg", "error")
        ).lower()
        return code == "51603" or "order does not exist" in message or "order not found" in message

    def _lookup_order_by_client_id(
        self,
        symbol: str,
        cl_ord_id: str,
        intent_id: str,
        attempts: int = 3,
        forget_not_found: bool = False,
    ):
        get_order = getattr(getattr(self, "_trade", None), "get_order", None)
        if not callable(get_order):
            return None
        for attempt in range(1, max(1, attempts) + 1):
            self._record_order_event(
                intent_id,
                "ORDER_LOOKUP_ATTEMPT",
                operation="get_order",
                attempt=attempt,
                payload={"instId": symbol, "clOrdId": cl_ord_id},
            )
            try:
                response = self._call(
                    get_order,
                    _max_attempts=1,
                    instId=symbol,
                    clOrdId=cl_ord_id,
                )
            except Exception as exc:
                self._record_order_event(
                    intent_id,
                    "ORDER_LOOKUP_EXCEPTION",
                    operation="get_order",
                    attempt=attempt,
                    message=str(exc),
                    payload={"instId": symbol, "clOrdId": cl_ord_id},
                )
                response = None
            if isinstance(response, dict):
                self._record_order_event(
                    intent_id,
                    "ORDER_LOOKUP_RESULT",
                    operation="get_order",
                    attempt=attempt,
                    status_code=str(response.get("code", "")),
                    message=str(response.get("msg", "")),
                    payload=response,
                )
                data = response.get("data") or []
                if str(response.get("code", "")) == "0" and data and isinstance(data[0], dict):
                    row = dict(data[0])
                    if row.get("ordId") or row.get("orderId"):
                        recovered = {"code": "0", "data": [row], "recovered": True}
                        self._record_order_event(
                            intent_id,
                            "ORDER_RECOVERED",
                            operation="get_order",
                            status_code="0",
                            payload=recovered,
                            intent_status="ACCEPTED",
                        )
                        self._forget_unknown(cl_ord_id)
                        return recovered
                if forget_not_found and self._is_order_not_found_response(response):
                    # This path is used only for persisted orders from an earlier
                    # process. A confirmed 51603 must not block new openings forever.
                    self._forget_unknown(cl_ord_id)
                    self._record_order_event(
                        intent_id,
                        "ORDER_CONFIRMED_MISSING",
                        operation="get_order",
                        status_code="51603",
                        message="exchange confirmed client order does not exist",
                        payload=response,
                        intent_status="REJECTED",
                    )
                    return None
            if attempt < attempts:
                getattr(self, "_sleep", time.sleep)(0.5 * attempt)
        return None

    def _bulk_prune_missing_unknown_orders(self, symbol: str):
        """Use the live-order snapshot to retire stale unknown intents in bulk.

        A process restart can leave hundreds of duplicate UNKNOWN intents. Querying
        each client id serially can starve the strategy loop. Any id absent from a
        successful live-order snapshot is no longer pending; position reconciliation
        remains the source of truth if it filled before leaving the live set.
        """
        get_order_list = getattr(getattr(self, "_trade", None), "get_order_list", None)
        if not callable(get_order_list):
            return None
        try:
            response = self._call(
                get_order_list,
                _max_attempts=1,
                instId=symbol,
                state="live",
            )
        except Exception as exc:
            self.log.warning("[Order] live-order snapshot failed for %s: %s", symbol, exc)
            return None
        if not isinstance(response, dict) or str(response.get("code", "")) != "0":
            return None
        rows = response.get("data") or []
        if not isinstance(rows, list):
            return None
        live_by_client_id = {
            str(row.get("clOrdId")): row
            for row in rows
            if isinstance(row, dict) and str(row.get("clOrdId") or "")
        }
        lock = getattr(self, "_unknown_orders_lock", None)
        if lock is None:
            return []
        with lock:
            pending = [
                dict(item)
                for item in getattr(self, "_unknown_orders", {}).values()
                if item.get("symbol") == symbol
            ]
        live_unknown = []
        for item in pending:
            cl_ord_id = item["cl_ord_id"]
            live_row = live_by_client_id.get(cl_ord_id)
            if live_row is not None:
                live_unknown.append({**item, "live_order": dict(live_row)})
                continue
            self._forget_unknown(cl_ord_id)
            self._record_order_event(
                item["intent_id"],
                "ORDER_CONFIRMED_MISSING",
                operation="get_order_list",
                status_code="not_live",
                message="client order absent from exchange live-order snapshot",
                payload={"instId": symbol, "clOrdId": cl_ord_id},
                intent_status="REJECTED",
            )
        return live_unknown

    def _opening_order_unknown_block(self, symbol: str):
        bulk_live_unknown = self._bulk_prune_missing_unknown_orders(symbol)
        if bulk_live_unknown is not None:
            if not bulk_live_unknown:
                return None
            item = bulk_live_unknown[0]
            live_order = item.get("live_order") or {}
            return {
                "sCode": "order_unknown",
                "sMsg": "previous order outcome is still unknown",
                "clOrdId": item["cl_ord_id"],
                "recoveredOrdId": str(live_order.get("ordId") or live_order.get("orderId") or ""),
                "unknown": True,
                "guard_blocked": True,
            }
        lock = getattr(self, "_unknown_orders_lock", None)
        if lock is None:
            return None
        with lock:
            pending = [
                dict(item)
                for item in getattr(self, "_unknown_orders", {}).values()
                if item.get("symbol") == symbol
            ]
        for item in pending:
            recovered = self._lookup_order_by_client_id(
                symbol,
                item["cl_ord_id"],
                item["intent_id"],
                attempts=1,
                forget_not_found=item["cl_ord_id"] in getattr(self, "_startup_unknown_order_ids", set()),
            )
            if recovered:
                normalized = self._normalize_order_result(recovered)
                return {
                    "sCode": "unknown_recovered",
                    "sMsg": "previous unknown order recovered; current opening order skipped",
                    "clOrdId": item["cl_ord_id"],
                    "recoveredOrdId": normalized.get("ordId", ""),
                    "guard_blocked": True,
                }
            with lock:
                still_pending = item["cl_ord_id"] in getattr(self, "_unknown_orders", {})
            if not still_pending:
                # The lookup explicitly confirmed this persisted intent is gone;
                # continue with the next historical unknown (or the new order).
                continue
            return {
                "sCode": "order_unknown",
                "sMsg": "previous order outcome is still unknown",
                "clOrdId": item["cl_ord_id"],
                "unknown": True,
                "guard_blocked": True,
            }
        return None

    @staticmethod
    def _is_ambiguous_order_response(response: Dict[str, Any]) -> bool:
        """Identify a submission whose exchange outcome is unknown."""
        if not isinstance(response, dict):
            return True
        if str(response.get("code", "")) == "0" or response.get("ordId"):
            return False
        message = " ".join(
            str(response.get(key, "") or "")
            for key in ("msg", "sMsg", "error")
        ).lower()
        return any(
            token in message
            for token in (
                "all operations failed",
                "timed out",
                "timeout",
                "connection",
                "network",
                "gateway",
            )
        )

    def place_order(self, symbol: str, side: str, sz: str,
                    ord_type: str = "market", px: str = None,
                    margin_mode: str = "cross",
                    pos_side: str = "", reduce_only: bool = False,
                    tgt_ccy: str = "") -> Dict:
        if not reduce_only:
            unknown_block = self._opening_order_unknown_block(symbol)
            if unknown_block:
                return unknown_block
        params = {
            "instId": symbol, "tdMode": margin_mode,
            "side": side, "ordType": ord_type, "sz": self._format_order_size(sz),
            "clOrdId": self._new_client_order_id(),
        }
        if ord_type == "limit" and px is not None and str(px) != "":
            params["px"] = str(px)
        if pos_side and str(pos_side) != "":
            params["posSide"] = pos_side
        if reduce_only:
            params["reduceOnly"] = "true"
        if tgt_ccy and str(tgt_ccy) != "":
            params["tgtCcy"] = tgt_ccy
        intent_id = self._create_order_intent(params)

        if ord_type == "limit" and not reduce_only:
            guard = getattr(self, "_execution_guard", None)
            if guard is not None:
                allowed, guard_reason = guard.check_order(symbol, side, ord_type, safe_float(px or 0), reduce_only)
                if not allowed:
                    self._record_order_event(
                        intent_id,
                        "LOCAL_REJECT",
                        operation="place_order",
                        status_code="maker_guard",
                        message=guard_reason,
                        payload={"request": params},
                        intent_status="BLOCKED",
                    )
                    self.log.warning(f"[????] skip {symbol} {side} limit px={px} reason={guard_reason}")
                    return {"sCode": "maker_guard", "sMsg": guard_reason, "guard_blocked": True}

        self.log.info("??? %s %s sz=%s type=%s%s" % (symbol, side, sz, ord_type, (" px="+str(px)) if px else ""))
        try:
            resp = self._call(self._trade.place_order, _event_intent_id=intent_id, **params)
        except Exception as exc:
            recovered = self._lookup_order_by_client_id(
                symbol, params["clOrdId"], intent_id
            )
            if recovered:
                result = self._normalize_order_result(recovered)
                self.log.warning(
                    "[Order] recovered clOrdId=%s ordId=%s"
                    % (params["clOrdId"], result.get("ordId", ""))
                )
                return result
            self._remember_unknown(params["clOrdId"], symbol, intent_id, persisted=False)
            self._record_order_event(
                intent_id,
                "ORDER_UNKNOWN",
                operation="place_order",
                message=str(exc),
                payload={"request": params, "error": str(exc)},
                intent_status="UNKNOWN",
            )
            return {
                "sCode": "order_unknown",
                "sMsg": str(exc),
                "clOrdId": params["clOrdId"],
                "unknown": True,
                "guard_blocked": True,
            }
        result = self._normalize_order_result(resp)
        if result.get("ordId"):
            self._record_order_event(
                intent_id,
                "ORDER_ACCEPTED",
                operation="place_order",
                status_code=str(result.get("sCode", "0")),
                payload=result,
                intent_status="ACCEPTED",
            )
            self.log.info("?? ordId=%s" % result["ordId"])
        else:
            if self._is_ambiguous_order_response(result):
                recovered = self._lookup_order_by_client_id(
                    symbol, params["clOrdId"], intent_id
                )
                if recovered:
                    normalized = self._normalize_order_result(recovered)
                    self.log.warning(
                        "[Order] recovered ambiguous clOrdId=%s ordId=%s"
                        % (params["clOrdId"], normalized.get("ordId", ""))
                    )
                    return normalized
                self._remember_unknown(params["clOrdId"], symbol, intent_id, persisted=False)
                self._record_order_event(
                    intent_id,
                    "ORDER_UNKNOWN",
                    operation="place_order",
                    status_code=str(result.get("sCode", result.get("code", ""))),
                    message=str(result.get("sMsg", result.get("msg", ""))),
                    payload={"request": params, "response": result},
                    intent_status="UNKNOWN",
                )
                return {
                    **result,
                    "sCode": "order_unknown",
                    "sMsg": "ambiguous exchange response; order status unresolved",
                    "clOrdId": params["clOrdId"],
                    "unknown": True,
                    "guard_blocked": not reduce_only,
                }
            self._record_order_event(
                intent_id,
                "ORDER_REJECTED",
                operation="place_order",
                status_code=str(result.get("sCode", resp.get("code", ""))),
                message=str(result.get("sMsg", resp.get("msg", ""))),
                payload=result,
                intent_status="REJECTED",
            )
            self.log.warning(
                "??????: code=%s msg=%s" % (result.get("sCode", resp.get("code")), result.get("sMsg", resp.get("msg")))
            )
        return result
    def _normalize_order_result(self, resp: Dict) -> Dict:
        if not isinstance(resp, dict):
            return {"data": resp}
        result = dict(resp)
        result["sCode"] = str(resp.get("code", result.get("sCode", "")) or result.get("sCode", ""))
        result["sMsg"] = str(resp.get("msg", result.get("sMsg", "")) or result.get("sMsg", ""))
        data = resp.get("data") or []
        if isinstance(data, list) and data and isinstance(data[0], dict):
            first = dict(data[0])
            for key in ("ordId", "orderId", "clOrdId", "tag"):
                if key in first and key not in result:
                    result[key] = first[key]
            if first.get("sCode") and not result.get("sCode"):
                result["sCode"] = first["sCode"]
            if first.get("sMsg") and not result.get("sMsg"):
                result["sMsg"] = first["sMsg"]
        return result

    def close_all(self, symbol: str, margin_mode: str = "cross"):
        self.log.info(f"?? {symbol or 'ALL'}")
        try:
            if symbol:
                self._call(self._trade.close_positions, instId=symbol, mgnMode=margin_mode)
                return
        except Exception as exc:
            self.log.warning(f"??????????????: {exc}")
        for pos in self.get_positions(symbol):
            inst = str(pos.get("instId") or symbol or "").strip()
            side = str(pos.get("posSide") or "").lower()
            qty = safe_float(pos.get("pos", 0))
            if not inst or qty <= 0 or side not in {"long", "short"}:
                continue
            order_side = "sell" if side == "long" else "buy"
            try:
                self.place_order(inst, order_side, str(qty), ord_type="market", pos_side=side, reduce_only=True)
            except Exception as exc:
                self.log.warning(f"??????? {inst} {side}: {exc}")


    def set_leverage(self, symbol: str, lever: int, mgn_mode: str = "cross"):
        self.log.info(f"设置杠杆 {symbol} {lever}x")
        response = self._call(
            self._account.set_leverage,
            instId=symbol,
            lever=str(lever),
            mgnMode=mgn_mode,
        )
        if not isinstance(response, dict) or str(response.get("code", "0")) != "0":
            raise RuntimeError(f"set leverage failed: {response}")
        return response

    # ---------- 内部 ----------

    def get_funding_rate(self, symbol: str) -> float:
        resp = self._call(self._public.get_funding_rate, instId=symbol)
        data = resp.get("data", [])
        if data: return float(data[0].get("fundingRate", 0))
        return 0.0

    def get_next_funding_time(self, symbol: str) -> int:
        resp = self._call(self._public.get_funding_rate, instId=symbol)
        data = resp.get("data", [])
        if data: return int(data[0].get("fundingTime", 0))
        return 0

    def _wait_for_api_slot_locked(self):
        clock = getattr(self, "_clock", time.monotonic)
        sleep = getattr(self, "_sleep", time.sleep)
        window = max(0.1, float(getattr(self, "_rate_limit_window", 2.0)))
        max_calls = max(1, int(getattr(self, "_rate_limit_max_calls", 8)))
        while True:
            now = clock()
            backoff_until = float(getattr(self, "_rate_backoff_until", 0.0))
            if now < backoff_until:
                sleep(backoff_until - now)
                continue
            self._call_times = [ts for ts in getattr(self, "_call_times", []) if now - ts < window]
            if len(self._call_times) < max_calls:
                self._call_times.append(now)
                return
            sleep(max(0.01, self._call_times[0] + window - now))

    def _record_api_failure(self) -> None:
        now = time.time()
        try:
            with self._api_failure_lock:
                window = [t for t in getattr(self, "_api_failure_window", []) if now - t < 120.0]
                window.append(now)
                self._api_failure_window = window
        except Exception:
            pass

    def api_degraded(self, seconds: float = 60.0, threshold: int = 3) -> bool:
        """最近 seconds 秒内 API 失败次数 >= threshold 视为降级窗口。

        降级窗口内持仓/挂单查询结果不可信（可能返回 code=0 data=[]），
        调用方不应据此做"仓位已平"等状态变更。"""
        try:
            now = time.time()
            window = [t for t in getattr(self, "_api_failure_window", []) if now - t < seconds]
            return len(window) >= threshold
        except Exception:
            return False

    def _set_api_backoff(self, seconds: float):
        clock = getattr(self, "_clock", time.monotonic)
        with self._lock:
            self._rate_backoff_until = max(
                float(getattr(self, "_rate_backoff_until", 0.0)),
                clock() + max(0.0, float(seconds)),
            )

    def _call(self, func: Callable, *args, **kwargs) -> dict:
        last_err = None
        intent_id = str(kwargs.pop("_event_intent_id", "") or "")
        max_attempts = max(1, int(kwargs.pop("_max_attempts", self._retries)))
        kwargs = {k: v for k, v in kwargs.items() if v is not None and v != ""}
        operation = str(getattr(func, "__name__", "") or "")
        api_role = self._sdk_api_role(func)
        request_count = int(getattr(self, "_sdk_request_count", 0) or 0)
        if api_role and request_count >= max(1000, int(getattr(self, "_sdk_recycle_threshold", 15000))):
            self._recycle_sdk_clients()
            func = self._sdk_method(api_role, operation) or func
        mutating = operation in {"place_order", "cancel_order", "close_positions", "set_leverage"}
        for attempt in range(1, max_attempts + 1):
            try:
                if mutating:
                    self._record_order_event(
                        intent_id,
                        "API_ATTEMPT",
                        operation=operation,
                        attempt=attempt,
                        payload={"request": kwargs},
                        intent_status="SUBMITTING" if intent_id else "",
                    )
                with self._lock:
                    self._wait_for_api_slot_locked()
                    # Some recovery/test clients are constructed without the
                    # normal __init__ path.  Keep the counter fail-safe so a
                    # missing observability field cannot break API calls.
                    self._sdk_request_count = int(getattr(self, "_sdk_request_count", 0) or 0) + 1
                    resp = func(*args, **kwargs)
                code = str(resp.get("code", "-1"))
                msg = resp.get("msg", "")
                if mutating:
                    self._record_order_event(
                        intent_id,
                        "API_RESULT",
                        operation=operation,
                        attempt=attempt,
                        status_code=code,
                        message=str(msg or ""),
                        payload=resp,
                    )
                if code != "0":
                    self.log.warning("API返回异常 [%s]: %s" % (code, msg))
                    self._record_api_failure()
                    if self._is_transport_failure(msg):
                        self._recycle_sdk_clients()
                        if api_role:
                            func = self._sdk_method(api_role, operation) or func
                    rate_limited = code == "50011" or "too many requests" in str(msg).lower()
                    transient = rate_limited or str(msg).lower() in {"all operations failed"}
                    if transient:
                        if operation == "place_order" and not rate_limited:
                            data = resp.get("data") or []
                            has_order_id = any(
                                isinstance(row, dict) and (row.get("ordId") or row.get("orderId"))
                                for row in data
                            )
                            if not has_order_id:
                                raise RuntimeError(f"ambiguous place_order response: {code} {msg}")
                        if rate_limited:
                            self._rate_limit_failures = min(5, int(getattr(self, "_rate_limit_failures", 0)) + 1)
                            delay = min(30.0, 2.0 ** self._rate_limit_failures)
                        else:
                            delay = float(self._retry_delay * attempt)
                        self._set_api_backoff(delay)
                        if attempt < max_attempts:
                            continue
                else:
                    self._rate_limit_failures = 0
                return resp
            except Exception as e:
                last_err = e
                if mutating:
                    self._record_order_event(
                        intent_id,
                        "API_EXCEPTION",
                        operation=operation,
                        attempt=attempt,
                        message=str(e),
                        payload={"request": kwargs, "error": str(e)},
                    )
                self.log.warning("API retry #%d: %s" % (attempt, e))
                if self._is_transport_failure(e):
                    self._recycle_sdk_clients()
                    if api_role:
                        func = self._sdk_method(api_role, operation) or func
                if operation == "place_order":
                    break
                if attempt < max_attempts:
                    self._set_api_backoff(self._retry_delay * attempt)
        raise last_err or RuntimeError("API call failed")

    def get_order_list(self, instId: str = "", state: str = "live") -> list:
        result = self._call(self._trade.get_order_list, instId=instId, state=state)
        if not result or str(result.get("code", "0")) != "0" or not isinstance(result.get("data", []), list):
            raise RuntimeError(f"order query failed: {result}")
        return result.get("data", [])
