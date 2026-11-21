
"""回测运行引擎"""
import math
from typing import List, Dict
from datetime import datetime


class BacktestResult:
    def __init__(self):
        self.initial_capital = 0.0
        self.final_equity = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.gross_profit = 0.0
        self.gross_loss = 0.0
        self.max_drawdown = 0.0
        self.peak_equity = 0.0
        self.equity_curve: List[float] = []
        self.trade_log: List[Dict] = []
        self.start_ts = 0
        self.end_ts = 0

    @property
    def roi(self) -> float:
        if self.initial_capital <= 0:
            return 0.0
        return (self.final_equity - self.initial_capital) / self.initial_capital * 100

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return self.gross_profit if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def sharpe(self) -> float:
        if len(self.equity_curve) < 50:
            return 0.0
        daily = []
        step = max(1, len(self.equity_curve) // 365)
        for i in range(step, len(self.equity_curve), step):
            prev = self.equity_curve[i - step]
            if prev > 0:
                ret = (self.equity_curve[i] - prev) / prev
                daily.append(ret)
        if len(daily) < 5:
            return 0.0
        avg = sum(daily) / len(daily)
        var = sum((r - avg) ** 2 for r in daily) / len(daily)
        std = math.sqrt(var) if var > 0 else 0.0001
        return (avg / std) * math.sqrt(365) if std > 0 else 0.0


class SpotGridBacktest:
    def __init__(self, total_invest=100.0, grid_count=5,
                 grid_step=0.01, tp_ratio=0.015,
                 init_ratio=0.2, fee_rate=0.001,
                 layer_mult=1.2):
        self.total_invest = total_invest
        self.grid_count = grid_count
        self.grid_step = grid_step
        self.tp_ratio = tp_ratio
        self.init_ratio = init_ratio
        self.fee_rate = fee_rate
        self.layer_mult = layer_mult

    def run(self, candles: List[dict]) -> BacktestResult:
        r = BacktestResult()
        r.initial_capital = self.total_invest
        r.start_ts = candles[0]["ts"] if candles else 0
        r.end_ts = candles[-1]["ts"] if candles else 0
        capital = self.total_invest
        btc = 0.0
        positions: List[Dict] = []
        r.peak_equity = capital

        for candle in candles:
            price = candle["close"]
            equity = capital + btc * price
            r.equity_curve.append(equity)
            if equity > r.peak_equity:
                r.peak_equity = equity
            dd = (r.peak_equity - equity) / r.peak_equity * 100
            if dd > r.max_drawdown:
                r.max_drawdown = dd

            if not positions:
                invest = self.total_invest * self.init_ratio
                if invest > capital:
                    invest = capital
                if invest <= 0:
                    continue
                fee = invest * self.fee_rate
                qty = (invest - fee) / price
                capital -= invest
                btc += qty
                positions.append({"price": price, "qty": qty, "cost": invest})
                continue

            if len(positions) < self.grid_count:
                last = positions[-1]["price"]
                if price <= last * (1 - self.grid_step):
                    used = sum(p["cost"] for p in positions)
                    base_invest = self.total_invest * self.init_ratio
                    idx = len(positions)
                    invest = base_invest * (self.layer_mult ** (idx - 1))
                    invest = min(invest, self.total_invest - used)
                    if invest > capital:
                        invest = capital
                    if invest <= 0:
                        continue
                    fee = invest * self.fee_rate
                    qty = (invest - fee) / price
                    capital -= invest
                    btc += qty
                    positions.append({"price": price, "qty": qty, "cost": invest})

            if positions:
                pos = positions[0]
                if price >= pos["price"] * (1 + self.tp_ratio):
                    sell_value = pos["qty"] * price
                    fee = sell_value * self.fee_rate
                    pnl = sell_value - fee - pos["cost"]
                    capital += sell_value - fee
                    btc -= pos["qty"]
                    r.total_trades += 1
                    if pnl > 0:
                        r.wins += 1
                        r.gross_profit += pnl
                    else:
                        r.losses += 1
                        r.gross_loss += abs(pnl)
                    r.trade_log.append({
                        "buy_price": pos["price"],
                        "sell_price": price,
                        "qty": pos["qty"],
                        "pnl": round(pnl, 2),
                        "ts": candle["ts"],
                    })
                    positions.pop(0)

        r.final_equity = capital + btc * (candles[-1]["close"] if candles else 0)
        return r


def format_result(r: BacktestResult) -> str:
    start = datetime.fromtimestamp(r.start_ts / 1000).strftime("%Y-%m-%d %H:%M")
    end = datetime.fromtimestamp(r.end_ts / 1000).strftime("%Y-%m-%d %H:%M")
    days = (r.end_ts - r.start_ts) / 86400000
    pnl = r.final_equity - r.initial_capital
    lines = []
    lines.append("=" * 55)
    lines.append("  OKX Quant Backtest Report")
    lines.append("=" * 55)
    lines.append(f"  Period:    {start} -> {end} ({days:.0f} days)")
    lines.append(f"  Capital:   ${r.initial_capital:.2f}")
    lines.append(f"  Final:     ${r.final_equity:.2f}")
    lines.append(f"  Return:    {r.roi:+.2f}% (${pnl:+.2f})")
    lines.append(f"  Max DD:    {r.max_drawdown:.2f}%")
    lines.append(f"  Sharpe:    {r.sharpe:.2f}")
    lines.append("")
    lines.append(f"  Trades:    {r.total_trades}")
    lines.append(f"  Win Rate:  {r.win_rate:.1f}%")
    lines.append(f"  Profit F:  {r.profit_factor:.2f}")
    lines.append("")
    if r.trade_log:
        lines.append("  Recent Trades:")
        for t in r.trade_log[-5:]:
            ts = datetime.fromtimestamp(t["ts"] / 1000).strftime("%m-%d %H:%M")
            sign = "+" if t["pnl"] >= 0 else ""
            lines.append(f"    {ts}  Buy@{t['buy_price']:.0f} -> Sell@{t['sell_price']:.0f}  {sign}${t['pnl']:.2f}")
    lines.append("=" * 55)
    return "\n".join(lines)
