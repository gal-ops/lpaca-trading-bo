"""Strategy 4: catalyst/news momentum (spec section 5). Never lets an
LLM-generated narrative alone trigger an order -- requires price
confirmation, elevated relative volume, and acceptable spread on top of
the sentiment score. The actual news-fetching/deduplication/sentiment-
classifier lives in a separate news client (not built yet); this module
consumes its output (context.news_score/news_headline/news_is_scheduled)
and decides whether to act on it."""

from __future__ import annotations

from alpaca_bot.regimes.indicators import atr
from alpaca_bot.strategies.base import (
    CandidateSignal,
    StrategyContext,
    TradePlan,
    ValidationResult,
    default_build_trade_plan,
    default_validate_candidate,
    new_signal_id,
)

NAME = "catalyst_news_momentum"
MATERIAL_SCORE_THRESHOLD = 3
MIN_RELATIVE_VOLUME = 2.5
MAX_SPREAD_PCT = 0.002
CONSOLIDATION_BARS = 4
DEFAULT_MAX_HOLDING_SECONDS = 2 * 3600.0
_NO_NEWS_HEADLINES = {"", "no recent news"}


class NewsMomentumStrategy:
    name = NAME

    def generate_candidate(self, context: StrategyContext) -> CandidateSignal | None:
        headline = (context.news_headline or "").strip().lower()
        if context.news_score is None or headline in _NO_NEWS_HEADLINES:
            return None
        if abs(context.news_score) < MATERIAL_SCORE_THRESHOLD:
            return None
        if (context.relative_volume or 0) < MIN_RELATIVE_VOLUME:
            return None
        if context.spread_pct is not None and context.spread_pct > MAX_SPREAD_PCT:
            return None
        if len(context.intraday_bars) < CONSOLIDATION_BARS:
            return None

        recent = context.intraday_bars[-CONSOLIDATION_BARS:]
        price_move = recent[-1].close - recent[0].close
        news_direction = 1 if context.news_score > 0 else -1
        market_agrees_with_news = (price_move > 0 and news_direction > 0) or (
            price_move < 0 and news_direction < 0
        )
        if not market_agrees_with_news:
            return None

        direction = "long" if news_direction > 0 else "short"
        if context.asset_class == "crypto" and direction == "short":
            return None  # spot-only, can't act on bearish news with a short

        entry = recent[-1].close
        daily_atr = atr(context.daily_bars)
        if daily_atr <= 0:
            daily_atr = max(b.high for b in recent) - min(b.low for b in recent)
        if daily_atr <= 0:
            return None

        if direction == "long":
            stop = entry - daily_atr
            target = entry + 2 * daily_atr
        else:
            stop = entry + daily_atr
            target = entry - 2 * daily_atr

        return CandidateSignal(
            signal_id=new_signal_id(NAME, context.symbol, direction),
            strategy=NAME, symbol=context.symbol, asset_class=context.asset_class,
            direction=direction, regime=context.regime, entry=entry, stop=stop, target=target,
            max_holding_seconds=DEFAULT_MAX_HOLDING_SECONDS,
            feature_snapshot={
                "news_score": context.news_score, "news_headline": context.news_headline,
                "news_is_scheduled": context.news_is_scheduled,
                "relative_volume": context.relative_volume, "spread_pct": context.spread_pct,
                "daily_atr": daily_atr,
            },
        )

    def validate_candidate(self, candidate: CandidateSignal, context: StrategyContext) -> ValidationResult:
        return default_validate_candidate(candidate)

    def build_trade_plan(self, candidate: CandidateSignal, context: StrategyContext) -> TradePlan | None:
        validation = self.validate_candidate(candidate, context)
        return default_build_trade_plan(candidate, validation)
