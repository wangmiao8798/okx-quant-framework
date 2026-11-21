"""隐马尔可夫模型市场状态检测"""
import numpy as np
from hmmlearn import hmm
from utils.logger import get_logger

HMM_LABELS = ["calm", "osc_weak", "osc_strong", "trend", "high_vol"]

class HMMDetector:
    def __init__(self, n_states=5):
        self.n_states = n_states
        self.model = None
        self._trained = False
        self.log = get_logger("hmm")

    def _extract_features(self, candles):
        """从K线提取特征: [log_ret, atr_ratio, vol_ratio, bb_pos]"""
        closes = np.array([c["close"] for c in candles], dtype=float)
        highs = np.array([c["high"] for c in candles], dtype=float)
        lows = np.array([c["low"] for c in candles], dtype=float)
        vols = np.array([c["vol"] for c in candles], dtype=float)
        n = len(closes)
        if n < 30: return np.array([])

        log_ret = np.diff(np.log(closes))
        atr = np.zeros(n)
        for i in range(1, n):
            tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
            atr[i] = (atr[i-1]*13 + tr) / 14 if i >= 14 else (atr[i-1]*i + tr)/(i+1) if i > 0 else tr
        atr_ratio = atr[1:] / closes[1:]
        avg_vol = np.mean(vols[-20:]) if n >= 20 else np.mean(vols)
        vol_ratio = vols[1:] / max(avg_vol, 1)
        sma = np.mean(closes[-20:]) if n >= 20 else np.mean(closes)
        bb_std = np.std(closes[-20:]) if n >= 20 else np.std(closes)
        bb_pos = (closes[1:] - sma) / max(bb_std, 0.01) if bb_std > 0 else np.zeros(n-1)

        features = np.column_stack([log_ret, atr_ratio, vol_ratio, bb_pos])
        features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
        return features

    def train(self, candles):
        features = self._extract_features(candles)
        if len(features) < 50: return False
        try:
            self.model = hmm.GaussianHMM(
                n_components=self.n_states,
                covariance_type="diag",
                n_iter=100,
                random_state=42
            )
            self.model.fit(features)
            self._trained = True
            means = self.model.means_
            state_order = np.argsort(np.std(means, axis=1))
            self._state_map = {old: new for new, old in enumerate(state_order)}
            self.log.info(f"HMM trained: {len(features)} samples, {self.n_states} states")
            return True
        except Exception as e:
            self.log.warning(f"HMM train failed: {e}")
            return False

    def predict(self, candles):
        """返回 (state_int, state_label, confidence)"""
        if not self._trained or not self.model:
            return 0, "unknown", 0.0
        features = self._extract_features(candles)
        if len(features) < 5: return 0, "unknown", 0.0
        try:
            states = self.model.predict(features)
            last_state = self._state_map.get(states[-1], states[-1])
            label = HMM_LABELS[last_state] if last_state < len(HMM_LABELS) else "unknown"
            probs = self.model.predict_proba(features)
            conf = float(max(probs[-1])) if len(probs) > 0 else 0.0
            return int(last_state), label, round(conf, 3)
        except Exception as e:
            self.log.warning(f"HMM predict failed: {e}")
            return 0, "unknown", 0.0

    @property
    def is_trained(self):
        return self._trained
