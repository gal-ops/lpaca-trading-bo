"""Shared technical indicators for the regime engines. Pure-Python, no
pandas/numpy dependency for these -- the series involved (a few hundred
daily bars for a handful of proxy symbols) are small enough that vectorized
math would only add a dependency, not speed."""

from __future__ import annotations

from alpaca_bot.data.bars import Bar


def ema_series(closes: list[float], period: int) -> list[float]:
    """Standard EMA. First value seeds from a simple average of the first
    `period` closes; returns one EMA value per close from that point on
    (shorter than the input by `period - 1`)."""
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    seed = sum(closes[:period]) / period
    out = [seed]
    for close in closes[period:]:
        out.append(close * k + out[-1] * (1 - k))
    return out


def ema_slope(closes: list[float], period: int, lookback: int = 3) -> float:
    """Sign/magnitude of the EMA's recent change -- positive means rising."""
    series = ema_series(closes, period)
    if len(series) <= lookback:
        return 0.0
    return series[-1] - series[-1 - lookback]


def true_range(bars: list[Bar]) -> list[float]:
    trs = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high, low = bars[i].high, bars[i].low
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return trs


def atr(bars: list[Bar], period: int = 14) -> float:
    trs = true_range(bars)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period


def adx14(bars: list[Bar], period: int = 14) -> float:
    """Standard Wilder ADX. Returns 0.0 if there isn't enough history."""
    if len(bars) < period * 2:
        return 0.0

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(bars)):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        prev_close = bars[i - 1].close
        trs.append(max(bars[i].high - bars[i].low, abs(bars[i].high - prev_close),
                        abs(bars[i].low - prev_close)))

    def wilder_smooth(values: list[float], period: int) -> list[float]:
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    smoothed_tr = wilder_smooth(trs, period)
    smoothed_plus_dm = wilder_smooth(plus_dm, period)
    smoothed_minus_dm = wilder_smooth(minus_dm, period)

    dx_values = []
    for tr_val, pdm, mdm in zip(smoothed_tr, smoothed_plus_dm, smoothed_minus_dm):
        if tr_val == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * (pdm / tr_val)
        minus_di = 100 * (mdm / tr_val)
        di_sum = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)

    if len(dx_values) < period:
        return sum(dx_values) / len(dx_values) if dx_values else 0.0
    return sum(dx_values[-period:]) / period


def intraday_vwap(intraday_bars: list[Bar]) -> float | None:
    """Session VWAP from a list of intraday (e.g. 5-min) bars covering the
    current session. Returns None if there's nothing to compute from."""
    if not intraday_bars:
        return None
    total_dollar = sum(b.close * b.volume for b in intraday_bars)
    total_volume = sum(b.volume for b in intraday_bars)
    return (total_dollar / total_volume) if total_volume else None


def realized_volatility(closes: list[float], lookback: int = 20) -> float:
    """Annualization-free realized vol: stdev of daily log-ish returns over
    the lookback window. Used only for percentile ranking against its own
    history, so units/annualization don't matter."""
    if len(closes) < lookback + 1:
        return 0.0
    returns = [(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - lookback, len(closes))
               if closes[i - 1]]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return variance ** 0.5


def percentile_of_latest(history: list[float]) -> float:
    """Where the last value in `history` ranks against the rest, as a
    0-100 percentile. Used for realized-vol percentile checks."""
    if len(history) < 2:
        return 50.0
    latest = history[-1]
    below_or_equal = sum(1 for v in history if v <= latest)
    return 100 * below_or_equal / len(history)
