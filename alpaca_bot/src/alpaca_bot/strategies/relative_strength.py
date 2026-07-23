"""Strategy 5: cross-sectional relative strength/weakness (spec section 5).
The actual continuous ranking across the eligible universe happens
upstream (universe/screening.py's rank_candidates + a per-symbol return
percentile computed by the caller); this module only decides whether a
given symbol's rank + regime + price structure clears the bar to trade.
Crypto raises its threshold (or stays in cash) during a bear regime rather
than shorting -- Alpaca crypto is spot-only."""

from __future__ import annotations

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

NAME = "cross_sectional_relative_strength"
EQUITY_LONG_PERCENTILE = 90     # top decile
EQUITY_SHORT_PERCENTILE = 10    # bottom decile
CRYPTO_LONG_PERCENTILE_NORMAL = 85
CRYPTO_LONG_PERCENTILE_BEAR = 95   # raised threshold during a crypto bear regime
DEFAULT_MAX_HOLDING_SECONDS = 4 * 3600.0


class RelativeStrengthStrategy:
    name = NAME

    def generate_candidate(self, context: StrategyContext) -> CandidateSignal | None:
        pct = context.cross_sectional_percentile
        if pct is None or not context.intraday_bars:
            return None
        latest = context.intraday_bars[-1]
        vwap = intraday_vwap(context.intraday_bars)

        if context.asset_class == "stock":
            if context.regime == "BULL_TREND" and pct >= EQUITY_LONG_PERCENTILE:
                if vwap is not None and latest.close <= vwap:
                    return None  # price-structure confirmation failed
                direction = "long"
            elif context.regime == "BEAR_TREND" and pct <= EQUITY_SHORT_PERCENTILE:
                if vwap is not None and latest.close >= vwap:
                    return None
                direction = "short"
            else:
                return None
        else:  # crypto: long-only, threshold rises in a bear regime
            threshold = (
                CRYPTO_LONG_PERCENTILE_BEAR if context.regime == "CRYPTO_BEAR_TREND"
                else CRYPTO_LONG_PERCENTILE_NORMAL
            )
            if pct < threshold:
                return None
            if vwap is not None and latest.close <= vwap:
                return None
            direction = "long"

        entry = latest.close
        atr_estimate = (latest.high - latest.low) * 3 or entry * 0.02
        if direction == "long":
            stop = entry - atr_estimate
            target = entry + 2 * atr_estimate
        else:
            stop = entry + atr_estimate
            target = entry - 2 * atr_estimate

        return CandidateSignal(
            signal_id=new_signal_id(NAME, context.symbol, direction),
            strategy=NAME, symbol=context.symbol, asset_class=context.asset_class,
            direction=direction, regime=context.regime, entry=entry, stop=stop, target=target,
            max_holding_seconds=DEFAULT_MAX_HOLDING_SECONDS,
            feature_snapshot={"cross_sectional_percentile": pct, "vwap": vwap},
        )

    def validate_candidate(self, candidate: CandidateSignal, context: StrategyContext) -> ValidationResult:
        return default_validate_candidate(candidate)

    def build_trade_plan(self, candidate: CandidateSignal, context: StrategyContext) -> TradePlan | None:
        validation = self.validate_candidate(candidate, context)
        return default_build_trade_plan(candidate, validation)
