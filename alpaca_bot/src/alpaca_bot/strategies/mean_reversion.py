"""Strategy 3: VWAP/statistical mean reversion (spec section 5). Only in
validated range regimes, with no unresolved material catalyst. Crypto is
long-only: buy downside reversion, never short an upside deviation."""

from __future__ import annotations

import statistics

from alpaca_bot.regimes.indicators import intraday_vwap
from alpaca_bot.strategies.base import (
    CandidateSignal,
    StrategyContext,
    TradePlan,
    ValidationResult,
    default_build_trade_plan,
    default_validate_candidate,
    new_signal_id,
)

NAME = "vwap_mean_reversion"
DEVIATION_THRESHOLD = 1.75
MATERIAL_CATALYST_SCORE = 3       # |news_score| at/above this vetoes reversion entirely
MIN_INTRADAY_BARS = 20
DEFAULT_MAX_HOLDING_SECONDS = 2 * 3600.0

_RANGE_REGIMES = {"RANGE", "CRYPTO_RANGE"}


class MeanReversionStrategy:
    name = NAME

    def generate_candidate(self, context: StrategyContext) -> CandidateSignal | None:
        if context.regime not in _RANGE_REGIMES:
            return None
        if len(context.intraday_bars) < MIN_INTRADAY_BARS:
            return None

        # Fresh catalysts often invalidate reversion setups entirely.
        if context.news_score is not None and abs(context.news_score) >= MATERIAL_CATALYST_SCORE:
            return None

        closes = [b.close for b in context.intraday_bars]
        vwap = intraday_vwap(context.intraday_bars)
        if vwap is None:
            return None
        stdev = statistics.pstdev(closes)
        if stdev == 0:
            return None

        prev_dev = (closes[-2] - vwap) / stdev
        latest_dev = (closes[-1] - vwap) / stdev
        feature_snapshot = {"vwap": vwap, "stdev": stdev, "prev_dev": prev_dev, "latest_dev": latest_dev}

        exhaustion_and_reverting_up = prev_dev <= -DEVIATION_THRESHOLD and latest_dev > prev_dev
        exhaustion_and_reverting_down = prev_dev >= DEVIATION_THRESHOLD and latest_dev < prev_dev

        if exhaustion_and_reverting_up:
            direction = "long"
            entry = closes[-1]
            recent_lows = [b.low for b in context.intraday_bars[-3:]]
            stop = min(recent_lows) * 0.995
            target = vwap
        elif exhaustion_and_reverting_down and context.asset_class == "stock":
            direction = "short"
            entry = closes[-1]
            recent_highs = [b.high for b in context.intraday_bars[-3:]]
            stop = max(recent_highs) * 1.005
            target = vwap
        else:
            return None

        return CandidateSignal(
            signal_id=new_signal_id(NAME, context.symbol, direction),
            strategy=NAME, symbol=context.symbol, asset_class=context.asset_class,
            direction=direction, regime=context.regime, entry=entry, stop=stop, target=target,
            max_holding_seconds=DEFAULT_MAX_HOLDING_SECONDS, feature_snapshot=feature_snapshot,
        )

    def validate_candidate(self, candidate: CandidateSignal, context: StrategyContext) -> ValidationResult:
        return default_validate_candidate(candidate)

    def build_trade_plan(self, candidate: CandidateSignal, context: StrategyContext) -> TradePlan | None:
        validation = self.validate_candidate(candidate, context)
        return default_build_trade_plan(candidate, validation)
