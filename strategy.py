import pandas as pd
import ta
import config


def compute_signals(bars) -> dict:
    """
    Aggressive strategy: catches both crossover signals AND momentum plays.

    BUY when ANY of:
      - EMA9 crosses above EMA21 AND RSI rising above 50 (trend reversal)
      - RSI was below 40 last bar, now above 40 (oversold bounce)

    SELL when ANY of:
      - EMA9 crosses below EMA21 AND RSI falling below 55
      - RSI rises above 75 (take profit on overbought spike)
    """
    if not bars or len(bars) < config.EMA_SLOW + 2:
        return {"signal": "hold", "rsi": None, "ema_fast": None, "ema_slow": None,
                "reason": "not enough bar history", "score": 0.0}

    closes = pd.Series([b.close for b in bars], dtype=float)

    ema_fast = ta.trend.EMAIndicator(closes, window=config.EMA_FAST).ema_indicator()
    ema_slow = ta.trend.EMAIndicator(closes, window=config.EMA_SLOW).ema_indicator()
    rsi = ta.momentum.RSIIndicator(closes, window=config.RSI_PERIOD).rsi()

    ef_now, ef_prev = ema_fast.iloc[-1], ema_fast.iloc[-2]
    es_now, es_prev = ema_slow.iloc[-1], ema_slow.iloc[-2]
    rsi_now = rsi.iloc[-1]
    rsi_prev = rsi.iloc[-2]

    crossed_above = ef_prev < es_prev and ef_now > es_now
    crossed_below = ef_prev > es_prev and ef_now < es_now
    rsi_bouncing = rsi_prev < 40 and rsi_now >= 40
    rsi_spiked = rsi_now >= 75

    signal = "hold"
    reason = "no signal — conditions not met"

    # BUY conditions
    if crossed_above and rsi_now >= config.RSI_OVERSOLD:
        signal = "buy"
        reason = f"EMA{config.EMA_FAST} crossed above EMA{config.EMA_SLOW}, RSI={rsi_now:.1f} (bullish reversal)"
    elif rsi_bouncing:
        signal = "buy"
        reason = f"RSI bounced from {rsi_prev:.1f}→{rsi_now:.1f} (oversold rebound)"

    # SELL conditions (override buy if both trigger — safety first)
    if crossed_below and rsi_now <= config.RSI_OVERBOUGHT:
        signal = "sell"
        reason = f"EMA{config.EMA_FAST} crossed below EMA{config.EMA_SLOW}, RSI={rsi_now:.1f} (bearish reversal)"
    elif rsi_spiked:
        signal = "sell"
        reason = f"RSI spiked to {rsi_now:.1f} (overbought, taking profit)"

    # Score used to rank competing buy candidates: how far RSI has moved off
    # neutral (50) plus how wide the EMA spread is, as a fraction of price.
    rsi_strength = abs(rsi_now - 50)
    ema_spread_pct = abs(ef_now - es_now) / es_now * 100 if es_now else 0
    score = rsi_strength + ema_spread_pct * 10

    return {
        "signal": signal,
        "rsi": round(float(rsi_now), 2),
        "ema_fast": round(float(ef_now), 4),
        "ema_slow": round(float(es_now), 4),
        "reason": reason,
        "score": round(float(score), 4),
    }


def rank_candidates(signals: dict, limit: int) -> list:
    """Given {symbol: result_dict} for symbols with signal == 'buy', return the
    top `limit` symbols ranked by score (strongest momentum/crossover first)."""
    buy_candidates = [(sym, r) for sym, r in signals.items() if r["signal"] == "buy"]
    buy_candidates.sort(key=lambda item: item[1]["score"], reverse=True)
    return [sym for sym, _ in buy_candidates[:limit]]


def calculate_qty(account, price: float, asset_class: str = "stock") -> float:
    """How many shares/coins to buy based on max position size.

    Crypto isn't marginable on Alpaca, so it can only be bought with actual
    cash headroom (non_marginable_buying_power), not the regular
    (margin-inclusive) buying_power used for stocks.
    """
    equity = float(account.equity)
    if asset_class == "crypto":
        # Leave a small buffer: our price is a few minutes old by the time the
        # order executes, so spending 100% of available cash can overshoot
        # and get rejected on a live price uptick.
        available = float(account.non_marginable_buying_power) * 0.97
    else:
        available = float(account.buying_power)
    max_spend = min(equity * config.MAX_POSITION_PCT, available)
    qty = max_spend / price
    return max(round(qty, 6), 0.000001)
