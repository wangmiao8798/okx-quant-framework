"""Kelly 仓位管理：根据历史胜率动态计算每笔投入"""
from utils.logger import get_logger

class KellySizer:
    def __init__(self, config: dict):
        kc = config.get("kelly", {})
        self.enabled = kc.get("enabled", True)
        self._default_k = kc.get("default_kelly", 0.25)
        self._max_k = kc.get("max_kelly", 0.5)
        self._min_k = kc.get("min_kelly", 0.02)
        self.trades = 0; self.wins = 0
        self._wins_sum = 0.0; self._loss_sum = 0.0
        self.avg_win = 0.0; self.avg_loss = 0.0

    def add_trade(self, pnl: float):
        self.trades += 1
        if pnl >= 0:
            self.wins += 1; self._wins_sum += pnl
            self.avg_win = self._wins_sum / max(self.wins, 1)
        else:
            self._loss_sum += abs(pnl)
            avg_l = self._loss_sum / max(self.trades - self.wins, 1)
            self.avg_loss = avg_l

    @property
    def win_rate(self) -> float:
        if self.trades < 3: return 0.5
        return self.wins / self.trades

    @property
    def profit_factor(self) -> float:
        if self.avg_loss < 0.01: return 2.0
        return self.avg_win / self.avg_loss

    @property
    def kelly_pct(self) -> float:
        if not self.enabled or self.trades < 3:
            return self._default_k
        wr = self.win_rate; pf = self.profit_factor
        k = (pf * wr - (1 - wr)) / pf
        return max(self._min_k, min(k, self._max_k))

    def size(self, available: float) -> float:
        return available * self.kelly_pct
