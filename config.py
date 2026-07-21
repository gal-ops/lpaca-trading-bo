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

# Strategy parameters — relaxed thresholds = more frequent trades
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_OVERSOLD = 50    # Buy on any momentum shift
RSI_OVERBOUGHT = 55  # Sell quickly to lock in gains

# Risk management — aggressive sizing
MAX_POSITION_PCT = 0.08   # Up to 8% of portfolio per position
MAX_POSITIONS = 12        # Up to 12 open positions at once (was 6)
STOP_LOSS_PCT = 0.05      # 5% stop loss
TAKE_PROFIT_PCT = 0.12    # 12% take profit target

# How many bars of historical data to fetch for indicators
LOOKBACK_BARS = 50
