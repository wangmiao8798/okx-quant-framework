"""
MOCA 自适应引擎 (Market Oscillation & Chaos Adaptive)

市场状态:
  OSCILLATE_STRONG  强震荡   网格加密，杠杆降低
  OSCILLATE_WEAK    弱震荡   适量操作
  TREND_UP          单边上涨  放大止盈，提高杠杆
  TREND_DOWN        单边下跌  降低杠杆，保守操作
  SPIKE             插针异动  全面收缩，降低风险
  HIGH_VOLATILITY   高波动   网格加密，缩小头寸
  CALM              低波动   高杠杆，窄网格（降低触达门槛）
"""

import time
from enum import Enum
from typing import Dict, List
from dataclasses import dataclass
from utils.logger import get_logger
from utils.helpers import calculate_atr, calculate_sma, calculate_std, calculate_adx, detect_wick_ratio


class MarketState(Enum):
    OSCILLATE_STRONG = "strong_oscillation"
    OSCILLATE_WEAK = "weak_oscillation"
    TREND_UP = "trending_up"
    TREND_DOWN = "trending_down"
    SPIKE = "spike"
    HIGH_VOLATILITY = "high_volatility"
    CALM = "calm"


@dataclass
class AdaptParams:
    grid_density: float = 1.0
    add_layers: float = 1.0
    leverage: float = 1.0
    tp_ratio: float = 1.0
    confidence: float = 0.0
    vol_step: float = 1.0
    atr_ratio: float = 0.0


class MOCAEngine:
    def __init__(self, config: dict):
        self.log = get_logger("core.moca")
        mc = config.get("moca", {})
        self._enabled = mc.get("enabled", True)
        self._atr_period = mc.get("atr_period", 14)
        self._bb_period = mc.get("bb_period", 20)
        self._bb_std = mc.get("bb_std", 2.0)
        th = mc.get("thresholds", {})
        self._high_vol = th.get("high_volatility", 0.03)
        self._extreme_vol = th.get("extreme_volatility", 0.08)
        self._trend_str = th.get("trend_strength", 25)
        self._vol_spike = th.get("volume_spike", 3.0)
        self._wick_ratio = th.get("wick_ratio", 2.5)
        ar = mc.get("adaptation_range", {})
        self._grid_r = ar.get("grid_density", [0.5, 2.0])
        self._lever_r = ar.get("leverage", [0.3, 2.0])
        self._layer_r = ar.get("add_layers", [0.5, 1.5])
        self._tp_r = ar.get("tp_ratio", [0.5, 2.0])
        atr_grid = mc.get("atr_grid_step", {}) or {}
        self._atr_grid_enabled = bool(atr_grid.get("enabled", True))
        self._atr_grid_reference_ratio = max(0.0001, float(atr_grid.get("reference_ratio", 0.01)))
        self._atr_grid_min_multiplier = max(0.5, float(atr_grid.get("min_multiplier", 0.75)))
        self._atr_grid_max_multiplier = max(
            self._atr_grid_min_multiplier,
            float(atr_grid.get("max_multiplier", 1.75)),
        )
        self._atr_grid_calm_max_multiplier = max(
            self._atr_grid_min_multiplier,
            min(self._atr_grid_max_multiplier, float(atr_grid.get("calm_max_multiplier", 1.0))),
        )
        self._state_history: List[MarketState] = []
        self._max_history = 5
        self._last_main_state = MarketState.CALM
        self._last_main_score = 0.0
        self._confirm_hits = 0
        self._state_hold_until = 0.0
        self._hysteresis = mc.get("state_hysteresis", 0.22)
        self._confirm_bars = int(mc.get("confirm_bars", 3))
        self._trend_margin = float(mc.get("trend_margin", 0.16))
        self._spike_margin = float(mc.get("spike_margin", 0.15))
        self.current_state = MarketState.CALM
        self.current_params = AdaptParams()
        self._cache: Dict[str, float] = {}
        self.last_update = 0

    def analyze(self, candles: List[Dict]) -> MarketState:
        """根据K线数据判断当前市场状态"""
        if len(candles) < max(self._atr_period, self._bb_period):
            return self.current_state
        c = [x["close"] for x in candles]
        h = [x["high"] for x in candles]
        l = [x["low"] for x in candles]
        v = [x["vol"] for x in candles]
        o = [x["open"] for x in candles]
        px = c[-1]
        atr = calculate_atr(h, l, c, self._atr_period)
        atr_r = atr / px if px > 0 else 0
        self._cache["atr_ratio"] = atr_r
        mid = calculate_sma(c, self._bb_period)
        sd = calculate_std(c, self._bb_period)
        upper = mid + self._bb_std * sd
        lower = mid - self._bb_std * sd
        bb_w = (upper - lower) / mid if mid > 0 else 0
        self._cache["bb_width"] = bb_w
        adx = calculate_adx(h, l, c, 14)
        self._cache["adx"] = adx
        avg_v = sum(v[-20:]) / max(len(v[-20:]), 1)
        vr = v[-1] / avg_v if avg_v > 0 else 1.0
        wr = detect_wick_ratio(h[-1], l[-1], o[-1], c[-1])
        pbb = (px - mid) / ((upper - lower) / 2) if upper > lower else 0
        roc3 = ((c[-1] - c[-4]) / c[-4]) if len(c) >= 4 and c[-4] else 0.0
        roc5 = ((c[-1] - c[-6]) / c[-6]) if len(c) >= 6 and c[-6] else 0.0

        score = {
            MarketState.TREND_UP: 0.0,
            MarketState.TREND_DOWN: 0.0,
            MarketState.OSCILLATE_STRONG: 0.0,
            MarketState.OSCILLATE_WEAK: 0.0,
            MarketState.SPIKE: 0.0,
            MarketState.HIGH_VOLATILITY: 0.0,
            MarketState.CALM: 0.0,
        }
        spike_score = 0.0
        trend_score = 0.0
        if atr_r >= self._extreme_vol * 1.15:
            spike_score += 0.8
        if atr_r >= self._high_vol * 1.05:
            score[MarketState.HIGH_VOLATILITY] += 0.7
        if wr >= self._wick_ratio * 1.1 and vr >= self._vol_spike * 1.15:
            spike_score += 0.65
        if adx >= self._trend_str:
            trend_score += min(1.0, (adx - self._trend_str) / max(self._trend_str, 1.0) + 0.2)
        if pbb > 0.72 and roc3 > 0 and roc5 > 0 and c[-1] > c[-3] > c[-5]:
            score[MarketState.TREND_UP] += 1.0 + min(0.3, pbb * 0.15)
        if pbb < -0.72 and roc3 < 0 and roc5 < 0 and c[-1] < c[-3] < c[-5]:
            score[MarketState.TREND_DOWN] += 1.0 + min(0.3, abs(pbb) * 0.15)
        if upper > px > lower:
            score[MarketState.OSCILLATE_STRONG] += 0.55 if bb_w > 0.03 else 0.8
            score[MarketState.OSCILLATE_WEAK] += 0.45 if bb_w <= 0.03 else 0.15
        if atr_r < self._high_vol * 0.45 and bb_w < 0.018:
            score[MarketState.CALM] += 0.9
        if trend_score > 0:
            score[MarketState.TREND_UP] += trend_score if roc3 > 0 and roc5 > 0 else 0.0
            score[MarketState.TREND_DOWN] += trend_score if roc3 < 0 and roc5 < 0 else 0.0
        if spike_score > 0:
            score[MarketState.SPIKE] += spike_score

        main = max(score, key=score.get)
        main_score = score[main]
        second_score = sorted(score.values(), reverse=True)[1] if len(score) > 1 else 0.0
        confidence = max(0.0, min(1.0, main_score - second_score + (adx / max(self._trend_str, 1.0)) * 0.1))

        if main == self._last_main_state:
            self._confirm_hits += 1
        else:
            self._confirm_hits = 1

        if self._state_history:
            last_hist = self._state_history[-1]
            if main == MarketState.TREND_UP and last_hist == MarketState.OSCILLATE_STRONG and confidence < 0.72:
                main = last_hist
            elif main == MarketState.TREND_DOWN and last_hist == MarketState.OSCILLATE_STRONG and confidence < 0.72:
                main = last_hist
            elif last_hist == MarketState.SPIKE and main != MarketState.SPIKE and confidence < 0.82:
                main = last_hist

        if self._confirm_hits < self._confirm_bars and main not in (MarketState.SPIKE, MarketState.HIGH_VOLATILITY):
            main = self.current_state

        self._state_history.append(main)
        if len(self._state_history) > self._max_history:
            self._state_history.pop(0)
        self._last_main_state = main
        self._last_main_score = main_score
        self.current_state = main
        self.last_update = time.time()
        self._cache["confidence"] = confidence
        self._cache["trend_score"] = trend_score
        self._cache["spike_score"] = spike_score
        return main

    def adapt(self) -> AdaptParams:
        """根据市场状态自适应调整参数"""
        p = AdaptParams()
        if not self._enabled:
            self.current_params = p
            return p
        atr_r = self._cache.get("atr_ratio", 0.01)
        vf = min(atr_r / self._high_vol, 2.0) if self._high_vol > 0 else 1.0
        s = self.current_state
        conf = 0.6

        if s == MarketState.OSCILLATE_STRONG:
            p.grid_density = self._lerp(self._grid_r, 1.0 + vf * 0.3)
            p.add_layers = self._lerp(self._layer_r, 1.2)
            p.leverage = self._lerp(self._lever_r, 1.0 / (1.0 + vf * 0.2))
            p.tp_ratio = self._lerp(self._tp_r, 0.8)
            conf = 0.7
        elif s == MarketState.OSCILLATE_WEAK:
            p.grid_density = self._lerp(self._grid_r, 0.8)
            p.add_layers = self._lerp(self._layer_r, 0.7)
            p.leverage = self._lerp(self._lever_r, 0.6)
            p.tp_ratio = self._lerp(self._tp_r, 1.2)
            conf = 0.5
        elif s == MarketState.TREND_UP:
            p.grid_density = self._lerp(self._grid_r, 0.7)
            p.add_layers = self._lerp(self._layer_r, 0.6)
            p.leverage = self._lerp(self._lever_r, 1.3)
            p.tp_ratio = self._lerp(self._tp_r, 1.5)
            conf = 0.75
        elif s == MarketState.TREND_DOWN:
            p.grid_density = self._lerp(self._grid_r, 0.7)
            p.add_layers = self._lerp(self._layer_r, 0.6)
            p.leverage = self._lerp(self._lever_r, 1.0)
            p.tp_ratio = self._lerp(self._tp_r, 1.3)
            conf = 0.75
        elif s == MarketState.SPIKE:
            p.grid_density = self._lerp(self._grid_r, 0.5)
            p.add_layers = self._lerp(self._layer_r, 0.5)
            p.leverage = self._lerp(self._lever_r, 0.3)
            p.tp_ratio = self._lerp(self._tp_r, 0.5)
            conf = 0.9
        elif s == MarketState.HIGH_VOLATILITY:
            p.grid_density = self._lerp(self._grid_r, 1.5)
            p.add_layers = self._lerp(self._layer_r, 0.8)
            p.leverage = self._lerp(self._lever_r, 0.5)
            p.tp_ratio = self._lerp(self._tp_r, 0.7)
            conf = 0.8
        elif s == MarketState.CALM:
            p.grid_density = self._lerp(self._grid_r, 1.8)
            p.add_layers = self._lerp(self._layer_r, 1.5)
            p.leverage = self._lerp(self._lever_r, 1.8)
            p.tp_ratio = self._lerp(self._tp_r, 1.8)
            conf = 0.6

        p.confidence = max(conf, float(self._cache.get("confidence", conf)))
        # ATR-driven grid width.  Calm markets use a narrower step so the
        # strategy can receive fills; volatile markets widen the step to avoid
        # fee-heavy churn.  The bounds keep this adaptive layer from changing
        # a configured grid by an unsafe amount.
        atr_r = max(0.0, float(self._cache.get("atr_ratio", 0.0) or 0.0))
        p.atr_ratio = atr_r
        if self._atr_grid_enabled and atr_r > 0:
            raw_multiplier = atr_r / self._atr_grid_reference_ratio
            if self.current_state == MarketState.CALM:
                p.vol_step = min(self._atr_grid_calm_max_multiplier, raw_multiplier)
            else:
                p.vol_step = raw_multiplier
            p.vol_step = max(
                self._atr_grid_min_multiplier,
                min(self._atr_grid_max_multiplier, p.vol_step),
            )
        else:
            p.vol_step = 1.0
        self.current_params = p
        self.log.info(f"MOCA state={s.value} density={p.grid_density:.2f} "
                      f"layers={p.add_layers:.2f} lever={p.leverage:.2f} "
                      f"tp={p.tp_ratio:.2f} conf={conf:.2f}")
        return p

    def should_break(self, candles: List[Dict]) -> bool:
        """检测是否需要熔断"""
        if len(candles) < 3:
            return False
        c = [x["close"] for x in candles[-3:]]
        for i in range(1, len(c)):
            if abs(c[i] - c[i-1]) / c[i-1] >= self._extreme_vol:
                return True
        h = [x["high"] for x in candles[-self._atr_period:]]
        l = [x["low"] for x in candles[-self._atr_period:]]
        cc = [x["close"] for x in candles[-self._atr_period:]]
        if len(cc) >= self._atr_period:
            a = calculate_atr(h, l, cc, self._atr_period)
            if a / cc[-1] >= self._extreme_vol * 2:
                return True
        return False

    @staticmethod
    def _lerp(r, t):
        lo, hi = r
        t = max(0.0, min(2.0, t))
        if t <= 1.0:
            return lo + (1.0 - lo) * t
        return 1.0 + (hi - 1.0) * (t - 1.0)

    def analyze_multi(self, tf_candles: dict) -> MarketState:
        """多时间框架分析：加权投票综合判断"""
        if not tf_candles:
            return self.current_state
        weights = {"1m":1,"5m":3,"15m":5,"1h":8,"4h":12,"1d":18}
        votes = {}
        for tf, candles in tf_candles.items():
            if not candles:
                continue
            state = self.analyze(candles)
            w = weights.get(tf.lower(), weights.get(tf, 2))
            votes[state] = votes.get(state, 0) + w
        if votes:
            ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
            main, main_w = ranked[0]
            second_w = ranked[1][1] if len(ranked) > 1 else 0
            gap = main_w - second_w
            if gap < 5 and self.current_state in (MarketState.TREND_UP, MarketState.TREND_DOWN):
                main = self.current_state
            self.current_state = main
            self.adapt()
            tfs = ",".join(tf_candles.keys())
            self.log.info(f"多时间框架 state={main.value} tfs=[{tfs}] gap={gap}")
        return self.current_state

    def state_label(self) -> str:
        labels = {
            MarketState.OSCILLATE_STRONG: "强震荡",
            MarketState.OSCILLATE_WEAK: "弱震荡",
            MarketState.TREND_UP: "单边上涨",
            MarketState.TREND_DOWN: "单边下跌",
            MarketState.SPIKE: "插针异动",
            MarketState.HIGH_VOLATILITY: "高波动",
            MarketState.CALM: "低波动",
        }
        return labels.get(self.current_state, "??")
