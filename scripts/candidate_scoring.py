"""Pure offline candidate scoring for the future one-way trend strategy.

The module only consumes normalized market observations.  It has no network,
database, exchange-client, or execution dependencies.  Every data-quality
failure is represented as a rejection so callers can safely use it in shadow
mode first.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
import statistics
from typing import Any, Mapping, Sequence


TIMEFRAME_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}


@dataclass(frozen=True)
class NormalizedCandle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    complete: bool = True


@dataclass(frozen=True)
class NormalizedOrderBook:
    timestamp_ms: int
    bid: float
    ask: float
    bid_depth_usdt: float
    ask_depth_usdt: float


@dataclass(frozen=True)
class NormalizedFunding:
    timestamp_ms: int
    rate: float


@dataclass(frozen=True)
class CandidateMarket:
    symbol: str
    candles: Mapping[str, Sequence[NormalizedCandle]]
    orderbook: NormalizedOrderBook
    funding: NormalizedFunding
    now_ms: int
    expected_move_pct: float | None = None
    stop_loss_pct: float | None = None


@dataclass(frozen=True)
class ScoringConfig:
    min_candles: int = 50
    min_depth_usdt: float = 100_000.0
    max_spread_pct: float = 0.05
    max_orderbook_age_ms: int = 15 * 60 * 1000
    max_funding_age_ms: int = 8 * 60 * 60 * 1000
    max_candle_age_bars: float = 2.0
    max_volatility_pct: float = 8.0
    max_adverse_funding: float = 0.0005
    min_reward_risk: float = 1.5
    min_score: int = 6


@dataclass(frozen=True)
class CandidateScore:
    symbol: str
    direction: str
    score: int
    eligible: bool
    reasons: tuple[str, ...]
    metrics: Mapping[str, float]
    signal_id: str
    valid_until_ms: int | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        result["metrics"] = dict(self.metrics)
        return result


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _candle(value: NormalizedCandle | Mapping[str, Any]) -> NormalizedCandle:
    if isinstance(value, NormalizedCandle):
        return value
    try:
        return NormalizedCandle(
            timestamp_ms=int(value.get("timestamp_ms", value.get("timestamp", value.get("ts", 0)))),
            open=float(value["open"]), high=float(value["high"]),
            low=float(value["low"]), close=float(value["close"]),
            volume=float(value.get("volume", value.get("vol", 0))),
            complete=bool(value.get("complete", True)),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return NormalizedCandle(0, math.nan, math.nan, math.nan, math.nan, math.nan, False)


def _orderbook(value: NormalizedOrderBook | Mapping[str, Any]) -> NormalizedOrderBook:
    if isinstance(value, NormalizedOrderBook):
        return value
    try:
        return NormalizedOrderBook(
            timestamp_ms=int(value.get("timestamp_ms", value.get("timestamp", value.get("ts", 0)))),
            bid=float(value["bid"]), ask=float(value["ask"]),
            bid_depth_usdt=float(value.get("bid_depth_usdt", value.get("bid_depth_notional", value.get("bid_depth", 0)))),
            ask_depth_usdt=float(value.get("ask_depth_usdt", value.get("ask_depth_notional", value.get("ask_depth", 0)))),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return NormalizedOrderBook(0, math.nan, math.nan, math.nan, math.nan)


def _funding(value: NormalizedFunding | Mapping[str, Any]) -> NormalizedFunding:
    if isinstance(value, NormalizedFunding):
        return value
    try:
        return NormalizedFunding(
            timestamp_ms=int(value.get("timestamp_ms", value.get("timestamp", value.get("ts", 0)))),
            rate=float(value.get("rate", value.get("funding_rate"))),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return NormalizedFunding(0, math.nan)


def _market(value: CandidateMarket | Mapping[str, Any]) -> CandidateMarket:
    if isinstance(value, CandidateMarket):
        return value
    candles = {
        timeframe: tuple(_candle(candle) for candle in rows)
        for timeframe, rows in (value.get("candles") or {}).items()
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes))
    }
    return CandidateMarket(
        symbol=str(value.get("symbol", "")), candles=candles,
        orderbook=_orderbook(value.get("orderbook", {})),
        funding=_funding(value.get("funding", {})),
        now_ms=int(value.get("now_ms", 0)),
        expected_move_pct=_number(value.get("expected_move_pct")),
        stop_loss_pct=_number(value.get("stop_loss_pct")),
    )


def _ema(values: Sequence[float], period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1.0 - alpha) * current
    return current


def _rsi(values: Sequence[float], period: int = 14) -> float:
    changes = [cur - prev for prev, cur in zip(values[-period - 1:-1], values[-period:])]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _atr_pct(rows: Sequence[NormalizedCandle], period: int = 14) -> float:
    recent = rows[-period:]
    true_ranges = []
    previous = rows[-period - 1].close
    for row in recent:
        true_ranges.append(max(row.high - row.low, abs(row.high - previous), abs(row.low - previous)))
        previous = row.close
    return sum(true_ranges) / period / rows[-1].close * 100.0


def _volatility_pct(rows: Sequence[NormalizedCandle], period: int = 20) -> float:
    recent = rows[-period - 1:]
    returns = [math.log(cur.close / prev.close) for prev, cur in zip(recent, recent[1:])]
    return statistics.pstdev(returns) * math.sqrt(period) * 100.0


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def score_candidate(
    market: CandidateMarket | Mapping[str, Any],
    direction: str,
    config: ScoringConfig | None = None,
) -> CandidateScore:
    """Score one direction.  Rejections are fail-closed and explainable."""
    cfg = config or ScoringConfig()
    data = _market(market)
    reasons: list[str] = []
    points = 0
    if direction not in {"long", "short"}:
        reasons.append("invalid_direction")
    if not data.symbol or data.now_ms <= 0:
        reasons.append("missing_data")

    timeframe_rows: dict[str, tuple[NormalizedCandle, ...]] = {}
    for timeframe, interval in TIMEFRAME_MS.items():
        rows = tuple(data.candles.get(timeframe, ()))
        timeframe_rows[timeframe] = rows
        if len(rows) < cfg.min_candles:
            _add_reason(reasons, "missing_data")
            continue
        if not rows[-1].complete:
            _add_reason(reasons, "incomplete_candle")
        if rows[-1].timestamp_ms > data.now_ms + 60_000:
            _add_reason(reasons, "invalid_timestamp")
        if data.now_ms - rows[-1].timestamp_ms > interval * cfg.max_candle_age_bars:
            _add_reason(reasons, "stale_data")
        for row in rows[-cfg.min_candles:]:
            values = (row.open, row.high, row.low, row.close, row.volume)
            if any(_number(value) is None for value in values) or row.close <= 0 or row.high < row.low:
                _add_reason(reasons, "missing_data")
                break

    book = data.orderbook
    funding = data.funding
    book_values = (book.bid, book.ask, book.bid_depth_usdt, book.ask_depth_usdt)
    if any(_number(value) is None for value in book_values) or book.bid <= 0 or book.ask < book.bid:
        _add_reason(reasons, "missing_data")
    else:
        if book.timestamp_ms > data.now_ms + 60_000:
            _add_reason(reasons, "invalid_timestamp")
        if data.now_ms - book.timestamp_ms > cfg.max_orderbook_age_ms:
            _add_reason(reasons, "stale_data")
        if book.bid_depth_usdt + book.ask_depth_usdt < cfg.min_depth_usdt:
            _add_reason(reasons, "thin_liquidity")
        if (book.ask - book.bid) / ((book.ask + book.bid) / 2.0) * 100.0 > cfg.max_spread_pct:
            _add_reason(reasons, "wide_spread")
    if _number(funding.rate) is None or funding.timestamp_ms <= 0:
        _add_reason(reasons, "missing_data")
    else:
        if funding.timestamp_ms > data.now_ms + 60_000:
            _add_reason(reasons, "invalid_timestamp")
        if data.now_ms - funding.timestamp_ms > cfg.max_funding_age_ms:
            _add_reason(reasons, "stale_data")

    metrics: dict[str, float] = {}
    signal_ts = max((rows[-1].timestamp_ms for rows in timeframe_rows.values() if rows), default=0)
    if not reasons or not any(reason == "missing_data" for reason in reasons):
        if all(len(rows) >= cfg.min_candles for rows in timeframe_rows.values()):
            closes = {tf: [row.close for row in rows] for tf, rows in timeframe_rows.items()}
            ema20 = {tf: closes[tf][-1] / _ema(closes[tf], 20) - 1.0 for tf in TIMEFRAME_MS}
            ema50 = {tf: closes[tf][-1] / _ema(closes[tf], 50) - 1.0 for tf in TIMEFRAME_MS}
            sign = 1.0 if direction == "long" else -1.0
            if all(sign * value > 0 for value in ema20.values()):
                points += 2
            else:
                _add_reason(reasons, "trend_misalignment")
            if all(sign * value > 0 for value in ema50.values()):
                points += 2
            else:
                _add_reason(reasons, "slow_trend_misalignment")
            rsi15 = _rsi(closes["15m"])
            metrics.update({"rsi15": rsi15, "ema20_edge_pct": abs(ema20["15m"]) * 100.0})
            if (direction == "long" and 42.0 <= rsi15 <= 70.0) or (direction == "short" and 30.0 <= rsi15 <= 58.0):
                points += 1
            volatility = max(_volatility_pct(timeframe_rows["15m"]), _atr_pct(timeframe_rows["15m"]))
            metrics["volatility_pct"] = volatility
            if volatility > cfg.max_volatility_pct:
                _add_reason(reasons, "extreme_volatility")
            volumes = [row.volume for row in timeframe_rows["15m"]]
            baseline = sum(volumes[-25:-5]) / 20.0
            volume_ratio = sum(volumes[-5:]) / 5.0 / baseline if baseline > 0 else 0.0
            metrics["volume_ratio"] = volume_ratio
            if volume_ratio >= 1.1:
                points += 1
            elif volume_ratio <= 0:
                _add_reason(reasons, "thin_liquidity")
            imbalance = (book.bid_depth_usdt - book.ask_depth_usdt) / max(book.bid_depth_usdt + book.ask_depth_usdt, 1e-12)
            metrics["book_imbalance"] = imbalance
            if (direction == "long" and imbalance >= 0.05) or (direction == "short" and imbalance <= -0.05):
                points += 1
            if (direction == "long" and funding.rate > cfg.max_adverse_funding) or (direction == "short" and funding.rate < -cfg.max_adverse_funding):
                _add_reason(reasons, "adverse_funding")
            atr = _atr_pct(timeframe_rows["15m"])
            reward = float(data.expected_move_pct) if data.expected_move_pct is not None else max(abs(ema20["15m"]) * 100.0, atr * 2.0)
            risk = float(data.stop_loss_pct) if data.stop_loss_pct is not None else max(atr * 1.25, 0.05)
            reward_risk = reward / risk if risk > 0 else 0.0
            metrics.update({"reward_pct": reward, "risk_pct": risk, "reward_risk": reward_risk})
            if reward_risk < cfg.min_reward_risk:
                _add_reason(reasons, "insufficient_reward_risk")
            else:
                points += 1

    eligible = not reasons and points >= cfg.min_score
    valid_until = signal_ts + 2 * TIMEFRAME_MS["15m"] if signal_ts else None
    signal_id = f"{data.symbol}:{signal_ts}:{direction}"
    return CandidateScore(data.symbol, direction, points, eligible, tuple(reasons), metrics, signal_id, valid_until)


def score_candidates(market: CandidateMarket | Mapping[str, Any], config: ScoringConfig | None = None) -> dict[str, CandidateScore]:
    """Evaluate long and short independently from the same snapshot."""
    return {direction: score_candidate(market, direction, config) for direction in ("long", "short")}


def shadow_signal_record(decision: CandidateScore, captured_at_ms: int | None = None) -> dict[str, Any]:
    """Return a serializable signal record; this function has no side effects."""
    return {
        "record_type": "shadow_signal",
        "mode": "offline_read_only",
        "execution_attempted": False,
        "captured_at_ms": captured_at_ms,
        **decision.to_dict(),
    }


def shadow_output(decisions: Sequence[CandidateScore], captured_at_ms: int | None = None) -> str:
    """Serialize shadow records as JSON without writing or executing anything."""
    return json.dumps([shadow_signal_record(item, captured_at_ms) for item in decisions], sort_keys=True)


__all__ = [
    "CandidateMarket", "CandidateScore", "NormalizedCandle", "NormalizedFunding",
    "NormalizedOrderBook", "ScoringConfig", "score_candidate", "score_candidates",
    "shadow_output", "shadow_signal_record",
]
