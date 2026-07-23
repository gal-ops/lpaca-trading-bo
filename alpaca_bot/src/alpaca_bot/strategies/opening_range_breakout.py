"""Strategy 1: opening-range breakout/breakdown (spec section 5). Equities
only, strong bull/bear regimes only. A conventional equity opening range
doesn't exist for crypto -- the spec explicitly says a separate 24/7
variant would need its own validation, which isn't built here."""

from __future__ import annotations

from alpaca_bot.regimes.indicators import atr, intraday_vwap
from alpaca_bot.strategies.base import (
    CandidateSignal,
    StrategyContext,
    TradePlan,
    ValidationResult,
    default_build_trade_plan,
    default_validate_candidate,
    new_signal_id,
)

NAME = "opening_range_breakout"
OPENING_RANGE_BARS = 3           # e.g. 3 x 5-min bars = 15 minutes
MIN_MINUTES_SINCE_OPEN = 15      # spec: wait until at least 9:45am ET
MAX_CHASE_ATR_MULTIPLE = 0.5
DEFAULT_MAX_HOLDING_SECONDS = 4 * 3600.0  # intraday only; session-close flatten still applies


class OpeningRangeBreakoutStrategy:
    name = NAME

    def generate_candidate(self, context: StrategyContext) -> CandidateSignal | None:
        if context.asset_class != "stock":
            return None
        if context.regime not in ("BULL_TREND", "BEAR_TREND"):
            return None
        if context.minutes_since_session_open is None or context.minutes_since_session_open < MIN_MINUTES_SINCE_OPEN:
            return None
        if len(context.intraday_bars) < OPENING_RANGE_BARS + 1:
            return None

        opening_bars = context.intraday_bars[:OPENING_RANGE_BARS]
        or_high = max(b.high for b in opening_bars)
        or_low = min(b.low for b in opening_bars)
        latest = context.intraday_bars[-1]
        vwap = intraday_vwap(context.intraday_bars)
        daily_atr = atr(context.daily_bars)
        volume_confirmed = (context.relative_volume or 0) >= 1.0

        feature_snapshot = {
            "or_high": or_high, "or_low": or_low, "latest_close": latest.close,
            "vwap": vwap, "daily_atr": daily_atr, "relative_volume": context.relative_volume,
        }

        if context.regime == "BULL_TREND":
            direction = "long"
            breakout = latest.close > or_high
            price_ok = vwap is None or latest.close > vwap
            rel_strength_ok = (context.cross_sectional_percentile or 0) > 50
            chase_distance = latest.close - or_high
            trigger = or_high
        else:
            direction = "short"
            breakout = latest.close < or_low
            price_ok = vwap is None or latest.close < vwap
            rel_strength_ok = (context.cross_sectional_percentile or 100) < 50
            chase_distance = or_low - latest.close
            trigger = or_low

        if not (breakout and price_ok and rel_strength_ok and volume_confirmed):
            return None
        if daily_atr > 0 and chase_distance > MAX_CHASE_ATR_MULTIPLE * daily_atr:
            return None  # too far beyond the trigger to chase

        entry = latest.close
        if direction == "long":
            stop = or_low
            target = entry + 2 * (entry - stop)
        else:
            stop = or_high
            target = entry - 2 * (stop - entry)

        return CandidateSignal(
            signal_id=new_signal_id(NAME, context.symbol, direction),
            strategy=NAME, symbol=context.symbol, asset_class=context.asset_class,
            direction=direction, regime=context.regime, entry=entry, stop=stop, target=target,
            max_holding_seconds=DEFAULT_MAX_HOLDING_SECONDS,
            feature_snapshot={**feature_snapshot, "trigger": trigger},
        )

    def validate_candidate(self, candidate: CandidateSignal, context: StrategyContext) -> ValidationResult:
        return default_validate_candidate(candidate)

    def build_trade_plan(self, candidate: CandidateSignal, context: StrategyContext) -> TradePlan | None:
        validation = self.validate_candidate(candidate, context)
        return default_build_trade_plan(candidate, validation)
