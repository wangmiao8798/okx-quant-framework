"""AI-Trader 交易平台客户端"""
import json, time, urllib.request, ssl
from utils.logger import get_logger

BASE = "https://ai4trade.ai/api"
CHALLENGE = "challenge-collab-v1-crypto-individual"

# OKX交易对 -> AI-Trader 交易对映射
SYMBOL_MAP = {
    "BTC-USDT": "BTC",
    "BTC-USDT-SWAP": "BTC",
    "ETH-USDT": "ETH",
    "ETH-USDT-SWAP": "ETH",
}

class AITraderClient:
    def __init__(self, config_path="config/ai_trader.json"):
        self.log = get_logger("aitrader")
        self._ready = False
        self._token = ""
        try:
            with open(config_path) as f:
                c = json.load(f)
            self._token = c.get("token", "")
            if self._token:
                self._ready = True
        except:
            pass
        self._ctx = ssl._create_unverified_context()

    @property
    def ready(self) -> bool:
        return self._ready

    def _headers(self):
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, data: dict = None) -> dict:
        url = BASE + path
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=self._headers(), method=method)
        try:
            r = urllib.request.urlopen(req, timeout=10, context=self._ctx)
            return json.load(r)
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:300]
            self.log.warning(f"AI-Trader API error {path}: {e.code} {err}")
            return {"error": err, "code": e.code}
        except Exception as e:
            self.log.warning(f"AI-Trader API error {path}: {e}")
            return {"error": str(e)}

    def get_portfolio(self) -> dict:
        return self._call("GET", f"/challenges/{CHALLENGE}/portfolio")

    def get_portfolio_value(self) -> float:
        r = self.get_portfolio()
        pf = r.get("portfolio", {})
        return pf.get("total_value", 0) or pf.get("cash", 0)

    def get_cash(self) -> float:
        r = self.get_portfolio()
        return r.get("portfolio", {}).get("cash", 0)

    def get_positions(self) -> list:
        r = self._call("GET", "/positions")
        return r.get("positions", [])

    def get_challenge_info(self) -> dict:
        return self._call("GET", f"/challenges/{CHALLENGE}")

    def _map_symbol(self, okx_symbol: str) -> str:
        return SYMBOL_MAP.get(okx_symbol, okx_symbol.split("-")[0])

    def buy(self, symbol: str, price: float, quantity: float) -> dict:
        """现货买入"""
        sym = self._map_symbol(symbol)
        return self._call("POST", f"/challenges/{CHALLENGE}/trade", {
            "side": "buy", "symbol": sym,
            "price": price, "quantity": quantity
        })

    def sell(self, symbol: str, price: float, quantity: float) -> dict:
        """现货卖出"""
        sym = self._map_symbol(symbol)
        return self._call("POST", f"/challenges/{CHALLENGE}/trade", {
            "side": "sell", "symbol": sym,
            "price": price, "quantity": quantity
        })

    def place_order(self, symbol: str, side: str, quantity: float,
                    price: float = 0, order_type: str = "market",
                    pos_side: str = "", reduce_only: bool = False,
                    margin_mode: str = "cash") -> dict:
        """
        通用下单方法，兼容引擎的 _execute_order 调用格式
        """
        if not self._ready:
            return {"error": "AI-Trader not ready", "code": -1}

        # 现货模式
        if margin_mode == "cash" or margin_mode == "":
            if side in ("buy", "buy_long"):
                return self.buy(symbol, price, quantity)
            else:
                return self.sell(symbol, price, quantity)

        # 合约模式 - 多空
        sym = self._map_symbol(symbol)
        if side in ("buy", "buy_long") or pos_side == "long":
            return self._call("POST", f"/challenges/{CHALLENGE}/trade", {
                "side": "buy", "symbol": sym,
                "price": price, "quantity": quantity,
                "leverage": 3, "pos_side": "long"
            })
        else:
            return self._call("POST", f"/challenges/{CHALLENGE}/trade", {
                "side": "sell", "symbol": sym,
                "price": price, "quantity": quantity,
                "leverage": 3, "pos_side": "short"
            })

    def close_position(self, symbol: str, pos_side: str = "") -> dict:
        """平仓"""
        sym = self._map_symbol(symbol)
        return self._call("POST", f"/challenges/{CHALLENGE}/trade", {
            "side": "sell" if pos_side in ("", "long") else "buy",
            "symbol": sym,
            "quantity": 0,
            "reduce_only": True,
            "close_all": True
        })
