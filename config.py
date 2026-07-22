import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

EXCEL_FILE = os.getenv("EXCEL_FILE", "trades.xlsx")

# Universe: the bot scans Alpaca's REAL tradable universe every cycle
# (alpaca_client.get_tradable_symbols), not a hardcoded watchlist. As of
# 2026-07-22 that's ~13,300 tradable US equities and 73 tradable crypto
# pairs. Stocks are filtered to major listed exchanges -- OTC/pink-sheet
# names are thin enough that 15-min bars are mostly noise, not signal.
# Crypto gets no exchange filter: the full Alpaca crypto list is used.
STOCK_EXCHANGE_ALLOWLIST = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}

# Bars are fetched in batched multi-symbol requests, not one call per
# symbol -- the only way scanning thousands of stocks stays fast. Chunk
# size is conservative to stay well clear of any URL-length/response-size
# limit; the delay between chunks keeps the whole cycle's call volume well
# under Alpaca's per-minute rate limit even across ~130+ chunks.
BAR_FETCH_CHUNK_SIZE = 100
BAR_FETCH_CHUNK_DELAY = 0.3

# After ranking buy candidates by technical score, only the top N get a
# news-sentiment lookup before final ranking -- bounds news-API call volume
# per cycle even on a market-wide rally with hundreds of raw signals, while
# staying far larger than MAX_POSITIONS so selection quality isn't
# compromised (the news score can still re-order who actually gets bought).
NEWS_CANDIDATE_CAP = 50

# Extended-hours stock trading: ~4am-8pm ET on trading days, well beyond the
# standard 9:30am-4pm ET core session. Still not literally 24/7 -- no US
# equity exchange is; crypto is the only asset class here that actually
# trades around the clock, with no gate at all. Doesn't check market
# holidays -- an order placed on one is simply rejected by Alpaca, a safe
# failure, not a silent bad trade.
#
# Alpaca REQUIRES limit orders (not market orders) outside the core session,
# because extended-session liquidity is much thinner -- a market order there
# could fill far from the intended price. EXTENDED_HOURS_LIMIT_BUFFER_PCT is
# the slippage buffer added to the reference price to keep a realistic
# chance of filling without accepting an open-ended bad fill.
EXTENDED_HOURS_ENABLED = True
EXTENDED_HOURS_START_ET = (4, 0)
EXTENDED_HOURS_END_ET = (20, 0)
EXTENDED_HOURS_LIMIT_BUFFER_PCT = 0.005

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
# size, keeping dollar risk per trade roughly comparable. This risk
# framework is unchanged by scanning a much bigger universe: MAX_POSITIONS
# still caps how many positions the bot can hold at once, so a larger
# candidate pool means better selection, not more concurrent risk.
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
