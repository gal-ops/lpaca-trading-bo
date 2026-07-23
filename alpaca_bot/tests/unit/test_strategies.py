"""Unit tests for the five strategy modules (spec section 5) and the
shared base interfaces they build on."""

from datetime import datetime, timedelta, timezone

from alpaca_bot.data.bars import Bar
from alpaca_bot.strategies.base import (
    CandidateSignal,
    default_build_trade_plan,
    default_validate_candidate,
    reward_risk_ratio,
)
from alpaca_bot.strategies.mean_reversion import MeanReversionStrategy
from alpaca_bot.strategies.news_momentum import NewsMomentumStrategy
from alpaca_bot.strategies.opening_range_breakout import OpeningRangeBreakoutStrategy
from alpaca_bot.strategies.relative_strength import RelativeStrengthStrategy
from alpaca_bot.strategies.vwap_pullback import VwapPullbackStrategy


def _bar(close, high=None, low=None, volume=100_000, ts=None, open_price=None):
    ts = ts or datetime.now(timezone.utc)
    return Bar(ts=ts, open=open_price if open_price is not None else close,
               high=high if high is not None else close + 0.1,
               low=low if low is not None else close - 0.1, close=close, volume=volume)


def _daily_trend(start, step, n, wide_range=2.0):
    base = datetime.now(timezone.utc) - timedelta(days=n)
    return [_bar(start + step * i, high=start + step * i + wide_range,
                 low=start + step * i - wide_range, ts=base + timedelta(days=i))
            for i in range(n)]


def _ctx(**overrides):
    from alpaca_bot.strategies.base import StrategyContext
    defaults = dict(
        symbol="AAPL", asset_class="stock", regime="BULL_TREND",
        daily_bars=_daily_trend(300, 1.0, 60), intraday_bars=[], benchmark_bars=[],
        relative_volume=1.0, minutes_since_session_open=30,
    )
    defaults.update(overrides)
    return StrategyContext(**defaults)


# ---- shared base helpers ----

def test_reward_risk_ratio_long():
    assert reward_risk_ratio(entry=100, stop=98, target=104, direction="long") == 2.0


def test_default_validate_rejects_crypto_short():
    sig = CandidateSignal(signal_id="x", strategy="s", symbol="BTC/USD", asset_class="crypto",
                           direction="short", regime="CRYPTO_BEAR_TREND", entry=100, stop=105,
                           target=90, max_holding_seconds=60)
    result = default_validate_candidate(sig)
    assert result.valid is False
    assert any("spot-only" in r for r in result.reasons)


def test_default_validate_rejects_weak_reward_risk():
    sig = CandidateSignal(signal_id="x", strategy="s", symbol="AAPL", asset_class="stock",
                           direction="long", regime="BULL_TREND", entry=100, stop=99, target=100.5,
                           max_holding_seconds=60)
    result = default_validate_candidate(sig)
    assert result.valid is False
    assert any("reward/risk" in r for r in result.reasons)


def test_default_build_trade_plan_none_when_invalid():
    sig = CandidateSignal(signal_id="x", strategy="s", symbol="AAPL", asset_class="stock",
                           direction="long", regime="BULL_TREND", entry=100, stop=99, target=100.5,
                           max_holding_seconds=60)
    assert default_build_trade_plan(sig, default_validate_candidate(sig)) is None


# ---- Strategy 1: opening-range breakout ----

def test_orb_generates_long_on_bull_breakout_with_volume():
    strat = OpeningRangeBreakoutStrategy()
    intraday = [_bar(100, high=101, low=99), _bar(100.5, high=101.5, low=99.5),
                _bar(101, high=101.8, low=100), _bar(102.5, high=102.6, low=102.0)]
    ctx = _ctx(regime="BULL_TREND", intraday_bars=intraday, relative_volume=1.5,
               cross_sectional_percentile=80)
    candidate = strat.generate_candidate(ctx)
    assert candidate is not None
    assert candidate.direction == "long"
    plan = strat.build_trade_plan(candidate, ctx)
    assert plan is not None
    assert plan.reward_risk >= 1.5


def test_orb_none_before_945_et_equivalent():
    strat = OpeningRangeBreakoutStrategy()
    ctx = _ctx(minutes_since_session_open=5, intraday_bars=[_bar(100)] * 4)
    assert strat.generate_candidate(ctx) is None


def test_orb_none_for_crypto():
    strat = OpeningRangeBreakoutStrategy()
    ctx = _ctx(asset_class="crypto", intraday_bars=[_bar(100)] * 4)
    assert strat.generate_candidate(ctx) is None


def test_orb_none_when_chasing_too_far_beyond_trigger():
    strat = OpeningRangeBreakoutStrategy()
    intraday = [_bar(100, high=101, low=99), _bar(100.2, high=101, low=99.5),
                _bar(100.1, high=101, low=100), _bar(130, high=130.5, low=129)]  # huge chase
    ctx = _ctx(intraday_bars=intraday, relative_volume=1.5, cross_sectional_percentile=80)
    assert strat.generate_candidate(ctx) is None


# ---- Strategy 2: VWAP pullback continuation ----

def test_vwap_pullback_long_setup():
    strat = VwapPullbackStrategy()
    intraday = [_bar(300, high=301, low=299) for _ in range(5)]
    # Bullish rejection candle: opens near the low, closes near the high.
    intraday[-1] = _bar(302, high=302.5, low=299.5, open_price=300, volume=200_000)
    ctx = _ctx(regime="BULL_TREND", daily_bars=_daily_trend(295, 0.15, 60), intraday_bars=intraday,
               relative_volume=1.5)
    candidate = strat.generate_candidate(ctx)
    assert candidate is not None
    assert candidate.direction == "long"


def test_vwap_pullback_none_for_crypto_short_setup():
    strat = VwapPullbackStrategy()
    ctx = _ctx(asset_class="crypto", regime="CRYPTO_BEAR_TREND", symbol="BTC/USD",
               daily_bars=_daily_trend(60000, -50, 60), intraday_bars=[_bar(59000)] * 5)
    assert strat.generate_candidate(ctx) is None


def test_vwap_pullback_none_when_pullback_too_deep():
    strat = VwapPullbackStrategy()
    daily = _daily_trend(300, 1.0, 60)  # ema20 near 350s
    intraday = [_bar(200, high=201, low=199) for _ in range(5)]  # way below ema20 -> invalidated
    ctx = _ctx(regime="BULL_TREND", daily_bars=daily, intraday_bars=intraday)
    assert strat.generate_candidate(ctx) is None


def test_vwap_pullback_none_outside_trend_regime():
    strat = VwapPullbackStrategy()
    ctx = _ctx(regime="RANGE", intraday_bars=[_bar(300)] * 5)
    assert strat.generate_candidate(ctx) is None


# ---- Strategy 3: mean reversion ----

def test_mean_reversion_long_on_oversold_bounce():
    strat = MeanReversionStrategy()
    intraday = [_bar(100) for _ in range(18)] + [_bar(90), _bar(92)]  # sharp dip then bounce
    ctx = _ctx(regime="RANGE", intraday_bars=intraday, news_score=None)
    candidate = strat.generate_candidate(ctx)
    assert candidate is not None
    assert candidate.direction == "long"


def test_mean_reversion_none_outside_range_regime():
    strat = MeanReversionStrategy()
    intraday = [_bar(100) for _ in range(18)] + [_bar(90), _bar(92)]
    ctx = _ctx(regime="BULL_TREND", intraday_bars=intraday)
    assert strat.generate_candidate(ctx) is None


def test_mean_reversion_none_with_material_catalyst():
    strat = MeanReversionStrategy()
    intraday = [_bar(100) for _ in range(18)] + [_bar(90), _bar(92)]
    ctx = _ctx(regime="RANGE", intraday_bars=intraday, news_score=4, news_headline="big news")
    assert strat.generate_candidate(ctx) is None


def test_mean_reversion_crypto_never_shorts_overbought():
    strat = MeanReversionStrategy()
    intraday = [_bar(100) for _ in range(18)] + [_bar(110), _bar(108)]  # spike then fade -> would be short
    ctx = _ctx(asset_class="crypto", symbol="BTC/USD", regime="CRYPTO_RANGE", intraday_bars=intraday)
    assert strat.generate_candidate(ctx) is None


# ---- Strategy 4: news momentum ----

def test_news_momentum_long_on_confirmed_positive_catalyst():
    strat = NewsMomentumStrategy()
    intraday = [_bar(100), _bar(101), _bar(102), _bar(104)]
    daily = _daily_trend(90, 0.5, 30)
    ctx = _ctx(daily_bars=daily, intraday_bars=intraday, relative_volume=3.0,
               news_score=4, news_headline="Company beats earnings", spread_pct=0.001)
    candidate = strat.generate_candidate(ctx)
    assert candidate is not None
    assert candidate.direction == "long"


def test_news_momentum_none_without_real_catalyst():
    strat = NewsMomentumStrategy()
    ctx = _ctx(intraday_bars=[_bar(100)] * 4, relative_volume=3.0,
               news_score=4, news_headline="no recent news")
    assert strat.generate_candidate(ctx) is None


def test_news_momentum_none_on_low_relative_volume():
    strat = NewsMomentumStrategy()
    ctx = _ctx(intraday_bars=[_bar(100), _bar(102)] * 2, relative_volume=1.0,
               news_score=4, news_headline="Company beats earnings")
    assert strat.generate_candidate(ctx) is None


def test_news_momentum_none_when_price_disagrees_with_news():
    strat = NewsMomentumStrategy()
    intraday = [_bar(104), _bar(103), _bar(102), _bar(100)]  # falling despite positive news
    ctx = _ctx(intraday_bars=intraday, relative_volume=3.0,
               news_score=4, news_headline="Company beats earnings")
    assert strat.generate_candidate(ctx) is None


def test_news_momentum_never_shorts_crypto_on_bad_news():
    strat = NewsMomentumStrategy()
    intraday = [_bar(100), _bar(98), _bar(96), _bar(94)]
    ctx = _ctx(asset_class="crypto", symbol="BTC/USD", intraday_bars=intraday, relative_volume=3.0,
               news_score=-4, news_headline="Exchange hack reported")
    assert strat.generate_candidate(ctx) is None


# ---- Strategy 5: cross-sectional relative strength ----

def test_relative_strength_long_top_decile_bull():
    strat = RelativeStrengthStrategy()
    intraday = [_bar(100), _bar(101), _bar(103)]
    ctx = _ctx(regime="BULL_TREND", intraday_bars=intraday, cross_sectional_percentile=95)
    candidate = strat.generate_candidate(ctx)
    assert candidate is not None
    assert candidate.direction == "long"


def test_relative_strength_short_bottom_decile_bear():
    strat = RelativeStrengthStrategy()
    intraday = [_bar(100), _bar(99), _bar(97)]
    ctx = _ctx(regime="BEAR_TREND", intraday_bars=intraday, cross_sectional_percentile=5)
    candidate = strat.generate_candidate(ctx)
    assert candidate is not None
    assert candidate.direction == "short"


def test_relative_strength_crypto_raises_threshold_in_bear():
    strat = RelativeStrengthStrategy()
    intraday = [_bar(60000), _bar(60100), _bar(60300)]
    ctx = _ctx(asset_class="crypto", symbol="BTC/USD", regime="CRYPTO_BEAR_TREND",
               intraday_bars=intraday, cross_sectional_percentile=90)  # would pass normal 85 threshold
    assert strat.generate_candidate(ctx) is None  # but not the raised 95 bear threshold


def test_relative_strength_none_below_percentile_threshold():
    strat = RelativeStrengthStrategy()
    ctx = _ctx(regime="BULL_TREND", intraday_bars=[_bar(100)] * 3, cross_sectional_percentile=60)
    assert strat.generate_candidate(ctx) is None
