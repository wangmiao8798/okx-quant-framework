"""权益基线健全性守护的单元测试（对应 futures_hedge._baseline_stale_reason / _handle_stale_baseline）。"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.futures_hedge import FuturesHedgeStrategy  # noqa: E402


class _LogStub:
    def warning(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def make_guard(baseline, ts=0.0, max_age=7.0, drift=0.30):
    s = FuturesHedgeStrategy.__new__(FuturesHedgeStrategy)
    s._risk_equity_baseline = baseline
    s._risk_equity_baseline_ts = ts
    s._baseline_max_age_days = max_age
    s._baseline_reanchor_drift = drift
    s._baseline_stale_log_ts = 0.0
    s.log = _LogStub()
    s._symbol = "TEST-USDT-SWAP"
    return s


def test_healthy_baseline_not_stale():
    s = make_guard(1004.45, ts=time.time())
    assert s._baseline_stale_reason(1000.0) == ""


def test_low_baseline_drift_detected():
    """基线远低于权益 → 旧事故：熔断永久静默失效。"""
    s = make_guard(300.0)
    reason = s._baseline_stale_reason(1000.0)
    assert reason.startswith("drift=")


def test_high_baseline_drift_detected():
    """基线远高于权益 → 旧事故：假熔断锁死（DRAM 1162 场景）。"""
    s = make_guard(1162.0)
    reason = s._baseline_stale_reason(250.0)
    assert reason.startswith("drift=")


def test_aged_baseline_detected():
    s = make_guard(1000.0, ts=time.time() - 30 * 86400)
    reason = s._baseline_stale_reason(1000.0)
    assert reason.startswith("age=")


def test_zero_baseline_not_evaluated():
    s = make_guard(0.0)
    assert s._baseline_stale_reason(1000.0) == ""


def test_within_drift_and_age_not_stale():
    s = make_guard(1020.0, ts=time.time())
    assert s._baseline_stale_reason(1000.0) == ""


def test_handle_stale_reanchors_when_not_halted():
    s = make_guard(300.0)
    s._is_hard_stop_active = lambda: False
    s._trading_halted = False
    s._save_pnl_data = lambda: None
    s._handle_stale_baseline(1000.0)
    assert s._risk_equity_baseline == 1000.0
    assert s._risk_equity_baseline_ts > 0


def test_handle_stale_keeps_baseline_during_hard_stop():
    """停机保护中绝不重锚——否则等于抹掉真实回撤记录。"""
    s = make_guard(300.0)
    s._is_hard_stop_active = lambda: True
    s._trading_halted = False
    s._save_pnl_data = lambda: None
    s._handle_stale_baseline(1000.0)
    assert s._risk_equity_baseline == 300.0


def test_handle_stale_keeps_baseline_when_halted():
    s = make_guard(300.0)
    s._is_hard_stop_active = lambda: False
    s._trading_halted = True
    s._save_pnl_data = lambda: None
    s._handle_stale_baseline(1000.0)
    assert s._risk_equity_baseline == 300.0


def test_handle_healthy_baseline_noop():
    s = make_guard(1004.45, ts=time.time())
    s._is_hard_stop_active = lambda: False
    s._trading_halted = False
    s._save_pnl_data = lambda: None
    s._handle_stale_baseline(1000.0)
    assert s._risk_equity_baseline == 1004.45
