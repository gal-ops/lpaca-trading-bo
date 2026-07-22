import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

EXCEL_FILE = os.getenv("EXCEL_FILE", "trades.xlsx")

# Stocks the bot will trade — high-volatility picks for aggressive plays
STOCK_WATCHLIST = [
    "NVDA", "TSLA", "AMD", "MSTR", "COIN",
    "PLTR", "HOOD", "RBLX", "SOFI", "SMCI",
    "AAPL", "MSFT", "GOOGL", "META", "AMZN",
]

# Crypto trades 24/7 — no market-hours gate applies to these
CRYPTO_WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD"]

# Strategy parameters
BAR_MINUTES = 15
EMA_FAST = 9
EMA_SLOW = 21
TREND_EMA = 50            # longer trend filter — only take buy signals when
                           # price is above this, so we're not buying dips
                           # inside a structural downtrend (falling knives)
RSI_PERIOD = 14
RSI_OVERSOLD = 50         # gate for EMA-cross buys: RSI must be at/above neutral
RSI_SELL_CEILING = 45     # gate for EMA-cross sells: RSI must be genuinely below
                           # neutral (was 55 — barely below neutral, which fired
                           # on ordinary pullbacks inside an uptrend and cut
                           # winners short; this was the main driver of losses)
RSI_BOUNCE_FLOOR = 32     # oversold-bounce buy trigger (was 40 — not a real dip)
RSI_SPIKE_SELL = 75       # take-profit on overbought spike

VOLUME_LOOKBACK = 20      # bars used for the rolling average-volume baseline
VOLUME_MULTIPLIER = 1.0   # entry bar's volume must be >= this x the baseline,
                           # to skip low-conviction/thin-participation moves

# News sentiment score (see news_client.py) at or below this vetoes a buy
# candidate, and independently triggers an early protective exit on any held
# position — same threshold both directions, so a symbol we'd refuse to buy
# today is also a symbol we don't keep holding once its news turns that bad.
NEWS_VETO_SCORE = -2

# Risk management — sized per asset class. Crypto's normal volatility
# routinely exceeds a stock's in a single day, so one shared stop-loss %
# (tuned for stocks) was getting crypto positions stopped out on ordinary
# noise. Crypto gets a wider stop/take-profit band but a smaller position
# size, keeping dollar risk per trade roughly comparable.
MAX_POSITION_PCT = 0.08         # stocks: up to 8% of portfolio per position
CRYPTO_MAX_POSITION_PCT = 0.04  # crypto: half-size, since its stop is wider
MAX_POSITIONS = 12
STOP_LOSS_PCT = 0.05
CRYPTO_STOP_LOSS_PCT = 0.09
TAKE_PROFIT_PCT = 0.12
CRYPTO_TAKE_PROFIT_PCT = 0.18

# How many bars of historical data to fetch for indicators — bumped up from
# 50 so TREND_EMA(50) has enough warmup room
LOOKBACK_BARS = 80
