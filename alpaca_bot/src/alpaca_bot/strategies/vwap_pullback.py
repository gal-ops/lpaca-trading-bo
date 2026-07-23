"""Strategy 2: VWAP pullback continuation (spec section 5). Works in
confirmed equity or crypto trends. Crypto is long-only (Alpaca crypto is
spot-only); equity shorts still need shortable/easy_to_borrow, which the
independent pre-trade validator (phase 9) re-checks against live broker
state before any order is submitted -- this module can't verify that
itself from bars alone."""

from __future__ import annotations

from alpaca_bot.regimes.indicators import ema_series, intraday_vwap
from alpaca_bot.strategies.base import (
    CandidateSignal,
    StrategyContext,
    TradePlan,
    ValidationResult,
    default_build_trade_plan,
    default_validate_candidate,
    new_signal_id,
)

NAME = "vwap_pullback_continuation"
NEAR_SUPPORT_TOLERANCE_PCT = 0.01
MAX_PULLBACK_DEPTH_PCT = 0.05     # deeper than this invalidates trend structure
DEFAULT_MAX_HOLDING_SECONDS = 4 * 3600.0

_TREND_DIRECTIONS = {
    "BULL_TREND": "long", "BEAR_TREND": "short",
    "CRYPTO_BULL_TREND": "long", "CRYPTO_BEAR_TREND": "short",
}


class VwapPullbackStrategy:
    name = NAME

    def generate_candidate(self, context: StrategyContext) -> CandidateSignal | None:
        direction = _TREND_DIRECTIONS.get(context.regime)
        if direction is None:
            return None
        if context.asset_class == "crypto" and direction == "short":
            return None  # crypto long-only

        closes = [b.close for b in context.daily_bars]
        if len(closes) < 21:
            return None
        ema9_series = ema_series(closes, 9)
        ema20_series = ema_series(closes, 20)
        if not ema9_series or not ema20_series:
            return None
        ema9, ema20 = ema9_series[-1], ema20_series[-1]

        if not context.intraday_bars:
            return None
        latest = context.intraday_bars[-1]
        vwap = intraday_vwap(context.intraday_bars)
        volume_confirmed = (context.relative_volume or 0) >= 1.0

        pullback_depth_pct = abs(latest.close - ema20) / ema20 if ema20 else 1.0
        if pullback_depth_pct > MAX_PULLBACK_DEPTH_PCT:
            return None  # too deep -- trend structure invalidated

        near_support = (
            (vwap is not None and abs(latest.low - vwap) / vwap < NEAR_SUPPORT_TOLERANCE_PCT)
            or abs(latest.low - ema20) / ema20 < NEAR_SUPPORT_TOLERANCE_PCT
        )
        midpoint = (latest.high + latest.low) / 2
        feature_snapshot = {
            "ema9": ema9, "ema20": ema20, "vwap": vwap,
            "relative_volume": context.relative_volume, "pullback_depth_pct": pullback_depth_pct,
        }

        if direction == "long":
            rejection_candle = latest.close > latest.open and latest.close > midpoint
            price_ok = vwap is None or latest.close > vwap
            if not (near_support and rejection_candle and price_ok and volume_confirmed):
                return None
            entry = latest.close
            stop = min(latest.low, ema20) * 0.995
            target = entry + 2 * (entry - stop)
        else:
            weak_relative_strength = (context.cross_sectional_percentile or 100) < 50
            rejection_candle = latest.close < latest.open and latest.close < midpoint
            price_ok = vwap is None or latest.close < vwap
            if not (near_support and rejection_candle and price_ok and volume_confirmed
                    and weak_relative_strength):
                return None
            entry = latest.close
            stop = max(latest.high, ema20) * 1.005
            target = entry - 2 * (stop - entry)

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
