from scripts.candidate_scoring import (
    CandidateMarket,
    NormalizedCandle,
    NormalizedFunding,
    NormalizedOrderBook,
    score_candidate,
    score_candidates,
    shadow_output,
)


NOW = 1_700_000_000_000


def candles(slope=0.5, volume=1_000.0, timestamp=NOW - 60 * 60 * 1000):
    rows = []
    price = 100.0
    for index in range(60):
        close = price + slope * index
        rows.append(NormalizedCandle(timestamp + index * 60_000, close - slope, close + 0.2, close - 0.2, close, volume))
    return tuple(rows)


def market(**overrides):
    item = {
        "symbol": "BTC-USDT-SWAP",
        "candles": {"15m": candles(), "1h": candles(), "4h": candles()},
        "orderbook": NormalizedOrderBook(NOW - 30_000, 129.99, 130.00, 300_000, 100_000),
        "funding": NormalizedFunding(NOW - 30_000, 0.00005),
        "now_ms": NOW,
        "expected_move_pct": 4.0,
        "stop_loss_pct": 1.0,
    }
    item.update(overrides)
    return CandidateMarket(**item)


def test_accepted_long_and_shadow_output_is_non_executable():
    decision = score_candidate(market(), "long")
    assert decision.eligible is True
    assert decision.score >= 6
    record = shadow_output([decision], NOW)
    assert '"execution_attempted": false' in record
    assert '"record_type": "shadow_signal"' in record


def test_accepted_short_is_scored_independently():
    data = market(
        candles={"15m": candles(-0.5), "1h": candles(-0.5), "4h": candles(-0.5)},
        orderbook=NormalizedOrderBook(NOW - 30_000, 99.99, 100.00, 100_000, 300_000),
        funding=NormalizedFunding(NOW - 30_000, -0.00005),
    )
    decisions = score_candidates(data)
    assert decisions["short"].eligible is True
    assert decisions["long"].eligible is False


def test_rejects_stale_thin_wide_and_extreme_inputs():
    bad_book = NormalizedOrderBook(NOW - 2_000_000, 99.0, 100.0, 10.0, 10.0)
    decision = score_candidate(market(orderbook=bad_book), "long")
    assert decision.eligible is False
    assert {"stale_data", "thin_liquidity", "wide_spread"} <= set(decision.reasons)

    volatile = []
    for index in range(60):
        close = 100.0 if index % 2 == 0 else 140.0
        volatile.append(NormalizedCandle(NOW - 60 * 60 * 1000 + index * 60_000, close, close + 1, close - 1, close, 1_000))
    extreme = score_candidate(
        market(candles={"15m": tuple(volatile), "1h": tuple(volatile), "4h": tuple(volatile)}),
        "long",
    )
    assert extreme.eligible is False
    assert "extreme_volatility" in extreme.reasons


def test_no_trade_when_reward_to_risk_is_insufficient_or_data_missing():
    decision = score_candidate(market(expected_move_pct=0.2, stop_loss_pct=1.0), "long")
    assert decision.eligible is False
    assert "insufficient_reward_risk" in decision.reasons
    missing = score_candidate(market(candles={}), "long")
    assert missing.eligible is False
    assert "missing_data" in missing.reasons
