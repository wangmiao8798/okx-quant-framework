"""趋势事件埋点测试：验证 _record_trend_event 落库、字段、异常安全。

transition（blocked/released 去重）逻辑在策略方法内用 getattr 跟踪，
本测试验证落库函数本身的正确性；函数不去重（多次调用均落库），
去重由调用方保证。
"""
import os
import sqlite3

import pytest

try:
    from strategies.futures_hedge import _record_trend_event
except Exception as e:  # pragma: no cover
    pytest.skip(f"cannot import strategies.futures_hedge: {e}", allow_module_level=True)


def _read(path, table):
    conn = sqlite3.connect(path, timeout=5)
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        cols = [d[0] for d in cur.description]
        return cols, cur.fetchall()
    finally:
        conn.close()


def test_moca_event_record(tmp_path):
    db = str(tmp_path / "trend_events.db")
    _record_trend_event("moca", "CRV-USDT-SWAP", db_path=db,
                        state="trending_up", confidence=0.81, apply_live=0)
    cols, rows = _read(db, "moca_events")
    assert len(rows) == 1
    assert rows[0][cols.index("symbol")] == "CRV-USDT-SWAP"
    assert rows[0][cols.index("state")] == "trending_up"
    assert abs(rows[0][cols.index("confidence")] - 0.81) < 1e-6
    assert rows[0][cols.index("apply_live")] == 0


def test_throttle_event_record(tmp_path):
    db = str(tmp_path / "trend_events.db")
    _record_trend_event("throttle", "RAM-USDT-SWAP", db_path=db,
                        side="short", action="blocked", net=305.0, soft=300.0)
    _record_trend_event("throttle", "RAM-USDT-SWAP", db_path=db,
                        side="short", action="released", net=120.0, soft=300.0)
    cols, rows = _read(db, "throttle_events")
    assert len(rows) == 2
    assert rows[0][cols.index("action")] == "blocked"
    assert rows[1][cols.index("action")] == "released"
    assert abs(rows[0][cols.index("net")] - 305.0) < 1e-6
    assert abs(rows[0][cols.index("soft")] - 300.0) < 1e-6


def test_record_is_exception_safe():
    bad = "/nonexistent_dir_xyz/nope/trend_events.db"
    try:
        _record_trend_event("moca", "X", db_path=bad, state="calm")
    except Exception:
        pytest.fail("record must be exception-safe and never raise")


def test_multiple_blocked_land_every_call(tmp_path):
    # 函数本身不去重（去重在调用方 getattr 跟踪），多次调用均落库
    db = str(tmp_path / "trend_events.db")
    for _ in range(3):
        _record_trend_event("throttle", "CRV-USDT-SWAP", db_path=db,
                            side="long", action="blocked", net=400.0, soft=300.0)
    _, rows = _read(db, "throttle_events")
    assert len(rows) == 3
