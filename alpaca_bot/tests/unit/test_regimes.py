"""Unit tests for the equity + crypto regime engines (spec section 4) and
their shared indicators."""

from datetime import datetime, timedelta, timezone

from alpaca_bot.data.bars import Bar
from alpaca_bot.regimes.crypto import classify_crypto_regime
from alpaca_bot.regimes.equity import classify_equity_regime
from alpaca_bot.regimes.indicators import adx14, ema_series, ema_slope, intraday_vwap, percentile_of_latest


def _bars(closes: list[float], high_offset=1.0, low_offset=1.0, volume=1_000_000) -> list[Bar]:
    base = datetime.now(timezone.utc) - timedelta(days=len(closes))
    return [
        Bar(ts=base + timedelta(days=i), open=c, high=c + high_offset, low=c - low_offset,
            close=c, volume=volume)
        for i, c in enumerate(closes)
    ]


def _trend(start: float, step: float, n: int) -> list[float]:
    return [start + step * i for i in range(n)]


def test_ema_series_length_and_seed():
    closes = [float(i) for i in range(1, 31)]
    series = ema_series(closes, 20)
    assert len(series) == 11
    assert series[0] == sum(closes[:20]) / 20


def test_ema_slope_positive_for_uptrend():
    closes = _trend(100, 1.0, 60)
    assert ema_slope(closes, 20) > 0


def test_ema_slope_negative_for_downtrend():
    closes = _trend(200, -1.0, 60)
    assert ema_slope(closes, 20) < 0


def test_adx14_higher_for_strong_trend_than_choppy_range():
    trending = _bars(_trend(100, 2.0, 60))
    choppy = _bars([100 + (i % 2) for i in range(60)])
    assert adx14(trending) > adx14(choppy)


def test_intraday_vwap_weighted_by_volume():
    bars = [
        Bar(ts=datetime.now(timezone.utc), open=100, high=101, low=99, close=100, volume=100),
        Bar(ts=datetime.now(timezone.utc), open=110, high=111, low=109, close=110, volume=300),
    ]
    vwap = intraday_vwap(bars)
    assert vwap == (100 * 100 + 110 * 300) / 400


def test_percentile_of_latest():
    assert percentile_of_latest([1, 2, 3, 4, 5]) == 100.0
    assert percentile_of_latest([5, 4, 3, 2, 1]) == 20.0


def test_equity_regime_bull_trend():
    closes = _trend(300, 1.0, 60)  # steady uptrend
    daily_bars = _bars(closes)
    # Intraday VWAP built from a lower price than the current close, so the
    # "price above VWAP" bull condition is unambiguously satisfied.
    intraday_bars = [Bar(ts=datetime.now(timezone.utc), open=closes[-1] - 2, high=closes[-1] - 1,
                          low=closes[-1] - 3, close=closes[-1] - 2, volume=1000)]
    result = classify_equity_regime(daily_bars, intraday_bars, breadth_pct=70.0)
    assert result.regime == "BULL_TREND"


def test_equity_regime_bear_trend():
    closes = _trend(400, -1.0, 60)  # steady downtrend
    daily_bars = _bars(closes)
    # Intraday VWAP built from a higher price than the current close, so the
    # "price below VWAP" bear condition is unambiguously satisfied.
    intraday_bars = [Bar(ts=datetime.now(timezone.utc), open=closes[-1] + 2, high=closes[-1] + 3,
                          low=closes[-1] + 1, close=closes[-1] + 2, volume=1000)]
    result = classify_equity_regime(daily_bars, intraday_bars, breadth_pct=25.0)
    assert result.regime == "BEAR_TREND"


def test_equity_regime_range_when_low_adx_and_neutral_breadth():
    closes = [300 + (i % 3) * 0.5 for i in range(60)]
    daily_bars = _bars(closes)
    result = classify_equity_regime(daily_bars, [], breadth_pct=50.0)
    assert result.regime == "RANGE"


def test_equity_regime_high_volatility_on_gap_shock():
    closes = _trend(300, 1.0, 60)
    daily_bars = _bars(closes)
    result = classify_equity_regime(daily_bars, [], breadth_pct=70.0, opening_gap=50.0)
    assert result.regime == "HIGH_VOLATILITY_OR_SHOCK"


def test_equity_regime_high_volatility_overrides_bull_setup():
    closes = _trend(300, 1.0, 60)
    daily_bars = _bars(closes)
    result = classify_equity_regime(daily_bars, [], breadth_pct=70.0, correlation_spike=True)
    assert result.regime == "HIGH_VOLATILITY_OR_SHOCK"


def test_equity_regime_insufficient_history_is_mixed():
    result = classify_equity_regime(_bars([100.0] * 10), [], breadth_pct=50.0)
    assert result.regime == "MIXED_OR_UNCERTAIN"


def test_crypto_regime_bull_trend():
    btc_closes = _trend(60000, 200.0, 30)
    eth_closes = _trend(3000, 20.0, 30)
    result = classify_crypto_regime(
        _bars(btc_closes), _bars(eth_closes),
        median_return_pct=1.5, pct_above_ema20=75.0,
    )
    assert result.regime == "CRYPTO_BULL_TREND"


def test_crypto_regime_bear_trend():
    btc_closes = _trend(60000, -200.0, 30)
    eth_closes = _trend(3000, -20.0, 30)
    result = classify_crypto_regime(
        _bars(btc_closes), _bars(eth_closes),
        median_return_pct=-1.5, pct_above_ema20=25.0,
    )
    assert result.regime == "CRYPTO_BEAR_TREND"


def test_crypto_regime_high_volatility_overrides():
    btc_closes = _trend(60000, 200.0, 30)
    eth_closes = _trend(3000, 20.0, 30)
    result = classify_crypto_regime(
        _bars(btc_closes), _bars(eth_closes),
        median_return_pct=1.5, pct_above_ema20=75.0, liquidity_deterioration=True,
    )
    assert result.regime == "CRYPTO_HIGH_VOLATILITY"


def test_crypto_regime_insufficient_history_is_mixed():
    result = classify_crypto_regime(_bars([100.0] * 5), _bars([100.0] * 5),
                                     median_return_pct=0, pct_above_ema20=50)
    assert result.regime == "CRYPTO_MIXED_OR_UNCERTAIN"
