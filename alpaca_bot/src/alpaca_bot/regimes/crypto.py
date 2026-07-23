"""Crypto market-regime classifier (spec section 4.2). Recalculated every
15 minutes. Alpaca crypto is spot-only (no shorting), so a bear regime
here means "hold cash/exit weak longs," never "open a short" -- that
constraint is enforced by the strategy layer (phase 6), not here.

Same priority-order convention as the equity classifier: CRYPTO_HIGH_
VOLATILITY is checked first as an abnormal-conditions override.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca_bot.data.bars import Bar
from alpaca_bot.regimes.indicators import ema_series, ema_slope, percentile_of_latest

CRYPTO_REGIMES = (
    "CRYPTO_BULL_TREND", "CRYPTO_BEAR_TREND", "CRYPTO_RANGE",
    "CRYPTO_HIGH_VOLATILITY", "CRYPTO_MIXED_OR_UNCERTAIN",
)


@dataclass
class CryptoRegimeResult:
    regime: str
    inputs: dict


def classify_crypto_regime(
    btc_bars: list[Bar],
    eth_bars: list[Bar],
    median_return_pct: float,
    pct_above_ema20: float,
    realized_vol_history: list[float] | None = None,
    correlation_spike: bool = False,
    liquidity_deterioration: bool = False,
) -> CryptoRegimeResult:
    btc_closes = [b.close for b in btc_bars]
    if len(btc_closes) < 21:
        return CryptoRegimeResult("CRYPTO_MIXED_OR_UNCERTAIN", {"reason": "insufficient BTC history"})

    btc_ema20_series = ema_series(btc_closes, 20)
    if not btc_ema20_series:
        return CryptoRegimeResult("CRYPTO_MIXED_OR_UNCERTAIN", {"reason": "insufficient EMA history"})

    btc_ema20 = btc_ema20_series[-1]
    btc_slope = ema_slope(btc_closes, 20)
    btc_close = btc_closes[-1]
    eth_closes = [b.close for b in eth_bars]
    eth_trend_up = len(eth_closes) >= 2 and eth_closes[-1] > eth_closes[0]

    vol_percentile = percentile_of_latest(realized_vol_history) if realized_vol_history else None

    inputs = {
        "btc_close": btc_close, "btc_ema20": btc_ema20, "btc_ema20_slope": btc_slope,
        "eth_trend_up": eth_trend_up, "median_return_pct": median_return_pct,
        "pct_above_ema20": pct_above_ema20, "vol_percentile": vol_percentile,
        "correlation_spike": correlation_spike, "liquidity_deterioration": liquidity_deterioration,
    }

    vol_shock = vol_percentile is not None and vol_percentile >= 90
    if vol_shock or correlation_spike or liquidity_deterioration:
        return CryptoRegimeResult("CRYPTO_HIGH_VOLATILITY", inputs)

    is_bull = (
        btc_close > btc_ema20 and btc_slope > 0 and eth_trend_up
        and median_return_pct > 0 and pct_above_ema20 >= 60
    )
    if is_bull:
        return CryptoRegimeResult("CRYPTO_BULL_TREND", inputs)

    is_bear = (
        btc_close < btc_ema20 and btc_slope < 0 and not eth_trend_up
        and median_return_pct < 0 and pct_above_ema20 <= 40
    )
    if is_bear:
        return CryptoRegimeResult("CRYPTO_BEAR_TREND", inputs)

    is_range = abs(btc_slope) < (btc_ema20 * 0.001) and 40 < pct_above_ema20 < 60
    if is_range:
        return CryptoRegimeResult("CRYPTO_RANGE", inputs)

    return CryptoRegimeResult("CRYPTO_MIXED_OR_UNCERTAIN", inputs)
