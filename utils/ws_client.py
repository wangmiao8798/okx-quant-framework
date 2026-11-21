#!/usr/bin/env python3
"""OKX WebSocket 行情客户端

v2 (2026-08-22):
- 在 tickers 频道基础上捕获 bidPx / askPx / bidSz / askSz 盘口数据
- 新增 get_quote() 接口: 返回 {last, bid, ask, age}, 过期返回 None
- 保持原 get_price()/is_connected 接口不变, 引擎旧路径零影响
"""
import json, time, threading

try:
    import websocket
except ImportError:
    websocket = None

from utils.logger import get_logger


def _f(v):
    """安全转 float, 空串/None 返回 0.0"""
    try:
        return float(v) if v not in (None, "") else 0.0
    except (ValueError, TypeError):
        return 0.0


class WSMarketClient:
    """WebSocket 行情订阅 - OKX V5 Public Channel (tickers)"""

    def __init__(self, symbol: str = "BTC-USDT-SWAP"):
        self.log = get_logger("ws_market")
        self._symbol = symbol
        self._latest_price = 0.0
        self._latest_bid = 0.0
        self._latest_ask = 0.0
        self._last_update = 0.0
        self._running = False
        self._ws = None
        self._thread = None
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._max_stale_seconds = 12
        self._url = "wss://ws.okx.com:8443/ws/v5/public"

    @property
    def latest_price(self) -> float:
        return self._latest_price

    @property
    def is_connected(self) -> bool:
        return self._latest_price > 0 and time.time() - self._last_update < self._max_stale_seconds

    @property
    def last_update_age(self) -> float:
        if self._last_update <= 0:
            return float("inf")
        return time.time() - self._last_update

    def get_quote(self):
        """返回 {last, bid, ask, age} 或 None (无数据/已过期)"""
        if not self.is_connected:
            return None
        last = self._latest_price
        bid = self._latest_bid or last
        ask = self._latest_ask or last
        return {
            "last": last,
            "bid": bid,
            "ask": ask,
            "spread": (ask - bid) / last if last > 0 else 0.0,
            "age": self.last_update_age,
        }

    def start(self):
        if websocket is None:
            self.log.warning("websocket-client 未安装, 使用REST回退")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log.info(f"WS行情启动: {self._symbol}")

    def stop(self):
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _run_loop(self):
        while self._running:
            try:
                self._connect()
            except Exception as e:
                self.log.warning(f"WS连接异常: {e}")
            if not self._running:
                break
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    def _connect(self):
        ws = websocket.WebSocketApp(
            self._url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever(reconnect=0, ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        self._reconnect_delay = 1
        self._last_update = time.time()
        sub = {
            "op": "subscribe",
            "args": [{"channel": "tickers", "instId": self._symbol}],
        }
        ws.send(json.dumps(sub))
        self.log.info(f"WS已连接: {self._symbol}")

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "data" in data:
                for tick in data["data"]:
                    last = _f(tick.get("last"))
                    if last:
                        self._latest_price = last
                        # OKX tickers 频道自带盘口; 空值时回退为 last
                        bid = _f(tick.get("bidPx"))
                        ask = _f(tick.get("askPx"))
                        self._latest_bid = bid if bid > 0 else last
                        self._latest_ask = ask if ask > 0 else last
                        self._last_update = time.time()
        except Exception:
            pass

    def _on_error(self, ws, error):
        self.log.warning(f"WS错误: {error}")

    def _on_close(self, ws, close_status_code, close_msg):
        self.log.info(f"WS断开: code={close_status_code}, age={self.last_update_age:.1f}s")


class WSClientManager:
    """管理多个WS连接, 每个交易对一个"""

    def __init__(self):
        self._clients = {}
        self._lock = threading.Lock()

    def subscribe(self, symbol: str) -> WSMarketClient:
        with self._lock:
            if symbol not in self._clients:
                client = WSMarketClient(symbol)
                self._clients[symbol] = client
                client.start()
            return self._clients[symbol]

    def get_price(self, symbol: str) -> float:
        with self._lock:
            client = self._clients.get(symbol)
            if client and client.is_connected:
                return client.latest_price
            return 0.0

    def get_quote(self, symbol):
        """返回该 symbol 的实时盘口 quote dict, 不可用返回 None"""
        with self._lock:
            client = self._clients.get(symbol)
            if client:
                return client.get_quote()
            return None

    def health_summary(self):
        """各 symbol 的 WS 健康度快照, 用于周期日志"""
        out = []
        with self._lock:
            for sym, c in self._clients.items():
                q = c.get_quote()
                out.append(
                    {
                        "symbol": sym,
                        "ok": q is not None,
                        "age": round(c.last_update_age, 1),
                        "spread": f"{q['spread'] * 100:.3f}%" if q else "n/a",
                    }
                )
        return out

    def stop_all(self):
        for client in self._clients.values():
            client.stop()

    @property
    def has_any(self) -> bool:
        return any(c.is_connected for c in self._clients.values())
