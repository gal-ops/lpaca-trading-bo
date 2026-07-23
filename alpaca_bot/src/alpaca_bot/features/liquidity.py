"""Cheap, inexpensive-to-compute features from daily bars alone (spec
section 6, step 4) -- used for the whole-universe prescreen before any
expensive per-symbol quote/model work happens."""

from __future__ import annotations

import statistics

from alpaca_bot.data.bars import Bar


def median_daily_dollar_volume(bars: list[Bar]) -> float:
    if not bars:
        return 0.0
    return statistics.median(b.close * b.volume for b in bars)


def average_daily_dollar_volume(bars: list[Bar]) -> float:
    if not bars:
        return 0.0
    values = [b.close * b.volume for b in bars]
    return sum(values) / len(values)


def average_volume(bars: list[Bar]) -> float:
    if not bars:
        return 0.0
    return sum(b.volume for b in bars) / len(bars)


def atr_pct(bars: list[Bar], period: int = 14) -> float:
    """Average True Range as a percentage of the latest close. Returns 0.0
    if there isn't enough history to compute a meaningful ATR."""
    if len(bars) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high, low = bars[i].high, bars[i].low
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    recent_tr = true_ranges[-period:]
    atr = sum(recent_tr) / len(recent_tr)
    last_close = bars[-1].close
    return (atr / last_close) if last_close else 0.0
