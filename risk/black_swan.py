import time, threading
from utils.logger import get_logger

class BlackSwanProtector:
    def __init__(self, config, on_trigger=None):
        self.log = get_logger("risk.blackswan")
        bc = config.get("risk",{}).get("black_swan",{})
        self._enabled = bc.get("enabled",True)
        self._j = bc.get("price_jump_threshold",0.05)
        self._c = bc.get("consecutive_drop",3)
        self._cool = bc.get("cooldown_seconds",600)
        self._on = on_trigger
        self._frozen = False; self._since = 0; self._bad = 0
        self._lock = threading.Lock()

    def check(self, price, prev):
        if not self._enabled or self._frozen: return False
        if prev <= 0: return False
        j = abs(price-prev)/prev
        if j >= self._j:
            self._bad += 1
            self.log.warning(f"黑天鹅: 波动{j:.2%} 连续{self._bad}")
        else:
            self._bad = max(0,self._bad-1); return False
        if self._bad >= self._c: self._freeze(); return True
        return False

    def check_candle(self, op, hi, lo, cl):
        if not self._enabled or self._frozen: return False
        if (hi-lo)/op >= self._j*2: self._freeze(); return True
        return False

    def _freeze(self):
        with self._lock: self._frozen=True; self._since=time.time(); self._bad=0
        self.log.warning(f"===== 黑天鹅熔断 休眠{self._cool}s =====")
        if self._on:
            try:
                self._on()
            except Exception as exc:
                # Keep the freeze active even if downstream handling fails.
                self.log.error(f"黑天鹅熔断回调失败: {exc}")

    def check_thaw(self):
        if not self._frozen: return True
        if time.time()-self._since >= self._cool:
            with self._lock: self._frozen=False
            self.log.info("系统解冻"); return True
        return False

    @property
    def is_frozen(self): return self._frozen

    @property
    def remaining(self):
        if not self._frozen: return 0
        return max(0,self._cool-(time.time()-self._since))

    def reset(self):
        with self._lock: self._frozen=False; self._bad=0
        self.log.info("黑天鹅已重置")
