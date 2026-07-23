"""Equity market-regime classifier (spec section 4.1). Transparent,
rule-based, recalculated at 9:45am ET and every 15 minutes through the
regular session. May later be upgraded to a hidden Markov model or
calibrated classifier, but this rule output is retained as an independent
validation layer even then (spec's own requirement).

Priority order (not specified explicitly in the spec, so documented here):
HIGH_VOLATILITY_OR_SHOCK is checked first, since it represents an
abnormal-conditions override that should pre-empt an ordinary trend/range
read, then BULL_TREND / BEAR_TREND / RANGE, with MIXED_OR_UNCERTAIN as the
catch-all for everything else.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca_bot.data.bars import Bar
from alpaca_bot.regimes.indicators import (
    adx14,
    atr,
    ema_series,
    ema_slope,
    intraday_vwap,
    percentile_of_latest,
)

EQUITY_REGIMES = (
    "BULL_TREND", "BEAR_TREND", "RANGE", "HIGH_VOLATILITY_OR_SHOCK", "MIXED_OR_UNCERTAIN",
)


@dataclass
class EquityRegimeResult:
    regime: str
    inputs: dict


def classify_equity_regime(
    spy_daily_bars: list[Bar],
    spy_intraday_bars: list[Bar],
    breadth_pct: float,
    daily_realized_vol_history: list[float] | None = None,
    opening_gap: float | None = None,
    correlation_spike: bool = False,
    dispersion_spike: bool = False,
) -> EquityRegimeResult:
    closes = [b.close for b in spy_daily_bars]
    if len(closes) < 51:
        return EquityRegimeResult("MIXED_OR_UNCERTAIN", {"reason": "insufficient SPY history"})

    ema20_series = ema_series(closes, 20)
    ema50_series = ema_series(closes, 50)
    if not ema20_series or not ema50_series:
        return EquityRegimeResult("MIXED_OR_UNCERTAIN", {"reason": "insufficient EMA history"})

    ema20, ema50 = ema20_series[-1], ema50_series[-1]
    slope20 = ema_slope(closes, 20)
    spy_close = closes[-1]
    vwap = intraday_vwap(spy_intraday_bars)
    adx_value = adx14(spy_daily_bars)
    daily_atr = atr(spy_daily_bars)

    vol_percentile = (
        percentile_of_latest(daily_realized_vol_history) if daily_realized_vol_history else None
    )

    inputs = {
        "spy_close": spy_close, "ema20": ema20, "ema50": ema50, "ema20_slope": slope20,
        "vwap": vwap, "adx14": adx_value, "daily_atr": daily_atr, "breadth_pct": breadth_pct,
        "opening_gap": opening_gap, "vol_percentile": vol_percentile,
        "correlation_spike": correlation_spike, "dispersion_spike": dispersion_spike,
    }

    gap_shock = (
        opening_gap is not None and daily_atr > 0 and abs(opening_gap) >= 1.25 * daily_atr
    )
    vol_shock = vol_percentile is not None and vol_percentile >= 90
    if gap_shock or vol_shock or correlation_spike or dispersion_spike:
        return EquityRegimeResult("HIGH_VOLATILITY_OR_SHOCK", inputs)

    is_bull = (
        spy_close > ema20 > ema50
        and slope20 > 0
        and (vwap is None or spy_close > vwap)
        and breadth_pct >= 60
    )
    if is_bull:
        return EquityRegimeResult("BULL_TREND", inputs)

    is_bear = (
        spy_close < ema20 < ema50
        and slope20 < 0
        and (vwap is None or spy_close < vwap)
        and breadth_pct <= 40
    )
    if is_bear:
        return EquityRegimeResult("BEAR_TREND", inputs)

    is_range = adx_value < 18 and 40 < breadth_pct < 60
    if is_range:
        return EquityRegimeResult("RANGE", inputs)

    return EquityRegimeResult("MIXED_OR_UNCERTAIN", inputs)
