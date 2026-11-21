"""
现货网格策略 v4 - 稳健版 + 风控增强
- 底仓20%，剩下80%分4次等跌加仓
- 每跌1%加一份，每份涨1.5%止盈
- 浮亏缩仓、K线防重复、价格去重
"""

import time
from typing import Dict, Optional
from dataclasses import dataclass
from strategies.base import BaseStrategy
from utils.helpers import safe_float
from utils.kelly import KellySizer


@dataclass
class Position:
    price: float
    quantity: float
    cost: float


class SpotGridStrategy(BaseStrategy):
    def __init__(self, config: dict, okx_client=None):
        super().__init__(config, "spot_grid")
        sc = config.get("spot", {})
        self._symbol = sc.get("symbol", "BTC-USDT")
        self._total_invest = safe_float(sc.get("invest_usdt", 100))
        self._init_ratio = safe_float(sc.get("first_layer_ratio", 0.2))
        self._grid_count = int(safe_float(sc.get("max_layers", 5)))
        self._grid_step = safe_float(sc.get("grid_step", 0.01))
        self._tp_ratio = safe_float(sc.get("take_profit_ratio", 0.015))
        self._layer_mult = safe_float(sc.get("layer_multiplier", 1.2))
        self._min_interval = sc.get("min_order_interval", 30)
        self._client = okx_client
        self._positions: list[Position] = []
        self._last_order = 0
        self._total_pnl = 0.0
        self._init_done = False
        self._min_qty = 0.00001
        self._qty_step = 0.00001
        self._fee_rate = 0.001
        self._db = None
        self._pending_buy = False
        # 基础参数（MOCA自适应用）
        self._base_grid_step = self._grid_step
        self._base_tp_ratio = self._tp_ratio
        self._base_grid_count = self._grid_count
        
        # === 改进1: 浮亏缩仓 ===
        self._floating_loss_shrink = safe_float(sc.get("floating_loss_shrink", 0.05))
        self._floating_loss_freeze = safe_float(sc.get("floating_loss_freeze", 0.12))
        self._floating_loss_ratio = 1.0
        
        # === 改进2: K线防重复 ===
        self._candle_anti_dup = sc.get("candle_anti_duplicate", True)
        self._last_buy_candle = ""
        self._last_sell_candle = ""
        self._current_candle_id = ""
        
        # === 改进3: 价格去重 ===
        self._price_dedup = sc.get("price_deduplication", True)
        self._kelly = KellySizer(config)

    def update_info(self, info: dict):
        self._min_qty = info.get("minSz", 0.00001)
        self._qty_step = info.get("lotSz", 0.00001)

    def _get_candle_id(self) -> str:
        """获取当前K线ID用于防重复"""
        if not self._candle_anti_dup or not self._client:
            return ""
        try:
            kline = self._client.get_candles(self._symbol, bar="5m", limit=1)
            if kline:
                return str(kline[0][0])
        except:
            pass
        return ""

    def _check_floating_loss(self, price: float) -> bool:
        """浮亏缩仓检查: 返回True=允许开单, False=禁止"""
        if not self._positions:
            self._floating_loss_ratio = 1.0
            return True
        total_cost = sum(p.cost for p in self._positions)
        total_qty = sum(p.quantity for p in self._positions)
        if total_qty <= 0 or total_cost <= 0:
            return True
        avg_price = sum(p.price * p.quantity for p in self._positions) / total_qty
        current_value = total_qty * price
        floating_pnl = (current_value - total_cost) / total_cost
        if floating_pnl < -self._floating_loss_freeze:
            self.log.warning(f"浮亏超限({floating_pnl:.1%}), 冻结开单")
            return False
        if floating_pnl < -self._floating_loss_shrink:
            self._floating_loss_ratio = 0.5
            self.log.info(f"浮亏缩仓({floating_pnl:.1%}), 仓位减半")
        else:
            self._floating_loss_ratio = 1.0
        return True

    def on_tick(self, ticker: dict) -> Optional[dict]:
        if not self._running or not self._enabled:
            return None
        price = safe_float(ticker.get("last", 0))
        if price <= 0:
            return None
        if not self._init_done:
            return self._init_position(price)
        
        # 更新当前K线ID
        self._current_candle_id = self._get_candle_id()
        
        # 浮亏缩仓检查
        if not self._check_floating_loss(price):
            return None
        
        if len(self._positions) < self._grid_count:
            action = self._check_buy(price)
            if action:
                return action
        return self._check_sell(price)

    def _init_position(self, price: float) -> Optional[dict]:
        invest = self._total_invest * self._init_ratio
        if self._client:
            try:
                invest = min(invest, max(0, self._client.get_balance("USDT")))
            except:
                pass
        if invest <= 0:
            return None
        qty = invest / price
        self._positions.append(Position(price, qty, invest))
        self._last_order = time.time()
        self._init_done = True
        self._pending_buy = True
        self._last_buy_candle = self._current_candle_id
        self.log.info(f"底仓 ${invest:.2f} ({qty:.6f} BTC) @ {price:.0f}")
        if self._notifier:
            self._notifier.trade("Spot Grid", "buy", f"{qty:.6f} BTC @ ${price:.0f}")
        self._save_state_internal()
        return {"action":"buy","symbol":self._symbol,"quantity":str(invest),"price":price,"tgt_ccy":"quote_ccy"}

    def _check_buy(self, price: float) -> Optional[dict]:
        if time.time() - self._last_order < self._min_interval:
            return None
        if price > self._positions[-1].price * (1 - self._grid_step):
            return None
        slots = self._grid_count - len(self._positions)
        if slots <= 0:
            return None
        
        # K线防重复: 同K线内只买一次
        if self._candle_anti_dup and self._current_candle_id and self._current_candle_id == self._last_buy_candle:
            return None
        
        # 价格去重: 同价位不重复挂单
        if self._price_dedup:
            for pos in self._positions:
                if abs(pos.price - price) / price < self._grid_step * 0.3:
                    return None
        
        # 金字塔加仓：第n层 = 基础金额 * 倍率^n
        base_invest = self._total_invest * self._init_ratio
        idx = len(self._positions)
        invest = min(base_invest * (self._layer_mult ** (idx - 1)), self._total_invest - sum(p.cost for p in self._positions))
        # 浮亏缩仓调整
        invest = invest * self._floating_loss_ratio
        # Kelly 仓位调整
        kelly_size = self._kelly.size(self._total_invest)
        invest = min(invest, kelly_size)
        if self._client:
            try:
                invest = min(invest, max(0, self._client.get_balance("USDT")))
            except:
                pass
        if invest <= 0:
            return None
        qty = invest / price * (1 - self._fee_rate)
        self._positions.append(Position(price, qty, invest))
        self._pending_buy = True
        self._last_order = time.time()
        self._last_buy_candle = self._current_candle_id
        self.log.info(f"加仓#{len(self._positions)} ${invest:.2f} ({qty:.6f} BTC) @ {price:.0f}")
        if self._notifier:
            self._notifier.trade("Spot Grid", "buy", f"#{len(self._positions)} ${invest:.2f} @ ${price:.0f}")
        self._save_state_internal()
        return {"action":"buy","symbol":self._symbol,"quantity":str(invest),"price":price,"tgt_ccy":"quote_ccy"}

    def _check_sell(self, price: float) -> Optional[dict]:
        if not self._positions:
            return None
        pos = self._positions[0]
        if (price - pos.price) / pos.price < self._tp_ratio:
            return None
        
        # K线防重复: 同K线内只卖一次
        if self._candle_anti_dup and self._current_candle_id and self._current_candle_id == self._last_sell_candle:
            return None
        
        sell_value = pos.quantity * price
        sell_fee = sell_value * self._fee_rate
        pnl = sell_value - sell_fee - pos.cost
        self._positions.pop(0)
        self._total_pnl += pnl
        self._kelly.add_trade(pnl)
        self._last_sell_candle = self._current_candle_id
        self._save_state_internal()
        self._last_order = time.time()
        self.log.info(f"止盈 +{pnl:.4f}U ({pos.quantity:.6f} BTC @ {price:.0f})")
        if self._notifier:
            self._notifier.trade("Spot Grid", "sell", f"{pos.quantity:.6f} BTC @ ${price:.0f} PnL=${pnl:.2f}")
        return {"action":"sell","symbol":self._symbol,"quantity":str(pos.quantity),"price":price,"pnl":pnl}

    def update_moca(self, params: dict):
        """MOCA自适应参数更新"""
        gd = params.get("grid_density", 1.0)
        al = params.get("add_layers", 1.0)
        tp = params.get("tp_ratio", 1.0)
        vs = params.get("vol_step", 1.0)
        # 24h波动率自适应步长
        self._grid_step = self._base_grid_step / max(gd, 0.3) * vs
        self._grid_count = max(2, int(self._base_grid_count * al))
        self._tp_ratio = max(0.005, self._base_tp_ratio * tp)
        self.log.info(f"MOCA调整: step={self._grid_step:.4f} count={self._grid_count} tp={self._tp_ratio:.4f}")

    def restore_state(self):
        if not self._db:
            return
        state = self._db.load_state("spot_grid")
        pos_data = state.get("positions", [])
        if pos_data:
            self._positions = [Position(p[0], p[1], p[2]) for p in pos_data]
            self._total_pnl = state.get("total_pnl", 0.0)
            self._init_done = True
            self.log.info(f"Restored: {len(self._positions)} positions, PnL={self._total_pnl:.2f}")

    def _save_state_internal(self):
        if not self._db:
            return
        state = {
            "positions": [[p.price, p.quantity, p.cost] for p in self._positions],
            "total_pnl": self._total_pnl,
            "init_done": self._init_done,
        }
        self._db.save_state("spot_grid", state)

    def on_order_result(self, result: dict):
        if result.get("side") == "buy" and result.get("sCode") != "0":
            if self._pending_buy and self._positions:
                self._positions.pop()
                self.log.warning(f"Order failed, removed position: {result.get('sMsg','')}")
            self._pending_buy = False

    def get_status(self) -> Dict:
        return {
            "strategy":"spot_grid","symbol":self._symbol,
            "running":self._running,"positions":len(self._positions),
            "pnl":round(self._total_pnl,2),"invest":self._total_invest,
            "floating_ratio":round(self._floating_loss_ratio,2),
        }
