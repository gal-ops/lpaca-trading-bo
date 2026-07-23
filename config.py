import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

EXCEL_FILE = os.getenv("EXCEL_FILE", "trades.xlsx")

# ============================================================
# PAPER vs LIVE — safety gate.
# ============================================================
# Defaults to False and MUST be flipped to True by hand, in code, to ever
# trade a real funded account. While False, alpaca_client forces the paper
# endpoint (paper=True) no matter what ALPACA_BASE_URL says, and the startup
# guard in main.test_connection() refuses to run if the configured endpoint
# looks live while this is False -- so a stray live URL in .env can never
# silently move real money. Flipping this to True is a deliberate, reviewed
# act; nothing here should do it automatically.
LIVE_TRADING = False

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

# ---- Position sizing (per the user's risk profile, 2026-07-23) ----
# Sized to simulate a ~€500 (~$540) account. IMPORTANT: reset the Alpaca
# paper balance to ~$540 so the percentage AND dollar limits below measure
# the same thing -- otherwise the dollar caps (tuned for €500) don't line up
# with a $142k paper balance and the daily-loss/drawdown stops misfire.
MAX_POSITION_PCT = 0.10         # stocks: up to 10% of the account per position
CRYPTO_MAX_POSITION_PCT = 0.05  # crypto: half-size, since its stop is wider

# Concentration caps. 5 positions x 10% = 50% max deployed, matching
# MAX_TOTAL_EXPOSURE_PCT, so the bot always keeps ~half the account in cash.
MAX_POSITIONS = 5
MAX_TOTAL_EXPOSURE_PCT = 0.50   # never let total positions exceed 50% of equity

# Absolute-dollar ceilings, applied IN ADDITION to the percentage caps, and
# the guardrail that actually matters on a small account. Sized to ~€500:
# MAX_POSITION_USD = 10% of ~$540; MAX_DAILY_LOSS_USD = 5% of ~$540. These
# assume the paper balance has been reset to ~$540 (see note above); on a
# larger balance, raise them to match or the daily-loss stop will trip daily.
MAX_POSITION_USD = 54           # hard cap on any single position's cost
MAX_DAILY_LOSS_USD = 27         # hard daily-loss ceiling (see kill switch below)

STOP_LOSS_PCT = 0.05
CRYPTO_STOP_LOSS_PCT = 0.09
TAKE_PROFIT_PCT = 0.12
CRYPTO_TAKE_PROFIT_PCT = 0.18

# How many bars of historical data to fetch for indicators — bumped up from
# 50 so TREND_EMA(50) has enough warmup room
LOOKBACK_BARS = 80

# Account-level daily-loss kill switch — the single most commonly cited gap
# in retail algo-trading risk management. If today's P&L drops past EITHER
# threshold (percentage of yesterday's close, OR an absolute dollar amount --
# whichever trips first), the bot stops opening NEW positions for the rest of
# the day. It does NOT stop managing existing positions -- stop-loss/take-
# profit/protective-news exits keep running. Uses Alpaca's own last_equity
# (previous trading day's close) as the baseline, so no state is needed.
MAX_DAILY_LOSS_PCT = 0.05       # 5% of yesterday's closing equity
# MAX_DAILY_LOSS_USD is defined in the position-sizing block above.

# ---- Intraday-only: flatten stock positions before the close (8A) ----
# The user wants no overnight STOCK risk: liquidate all stock positions a few
# minutes before the regular-session close, and stop opening new stock
# positions after the cutoff. Crypto is EXEMPT (24/7, no close) -- it is
# governed by its stops, the daily-loss switch, and the account drawdown stop.
INTRADAY_FLATTEN_STOCKS = True
FLATTEN_BEFORE_CLOSE_ET = (15, 55)   # 3:55pm ET -- flatten stocks at/after this
STOP_NEW_STOCK_BUYS_AFTER_ET = (15, 45)  # no new stock entries this late in the day

# ---- Account-level drawdown stop (9B) ----
# If account equity falls this far below its high-water mark, liquidate the
# ENTIRE book to cash and stop opening new positions for the rest of the day.
# The high-water mark is persisted in STATE_FILE (committed back by the
# workflow). When the stop fires it RESETS the high-water mark to the post-
# liquidation equity, so the account gets a genuine fresh start and resumes
# trading the next day from the reduced balance rather than immediately
# re-triggering. Set to 0 to disable.
ACCOUNT_DRAWDOWN_STOP_PCT = 0.10     # -10% from high-water mark
STATE_FILE = os.getenv("BOT_STATE_FILE", "bot_state.json")

# ---- Email alerts (10B) ----
# Sent on every fill and every circuit-breaker/drawdown trigger. All values
# come from env/secrets; if ALERT_EMAIL_TO or the SMTP creds are unset,
# alert_client no-ops silently so the bot still runs. Use an app-password
# (e.g. a Gmail app password) as ALERT_SMTP_PASSWORD, never your real password.
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
ALERT_SMTP_HOST = os.getenv("ALERT_SMTP_HOST", "smtp.gmail.com")
ALERT_SMTP_PORT = int(os.getenv("ALERT_SMTP_PORT", "587"))
ALERT_SMTP_USER = os.getenv("ALERT_SMTP_USER", "")
ALERT_SMTP_PASSWORD = os.getenv("ALERT_SMTP_PASSWORD", "")
