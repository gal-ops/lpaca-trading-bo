from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, AssetStatus
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.common.enums import Sort
from datetime import datetime, timedelta
import time
import config


trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=True)
stock_data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
crypto_data_client = CryptoHistoricalDataClient()  # crypto market data is public, no keys needed


def get_account():
    return trading_client.get_account()


def get_positions():
    positions = trading_client.get_all_positions()
    return {p.symbol: p for p in positions}


def get_open_orders():
    return trading_client.get_orders()


BAR_TIMEFRAME = TimeFrame(config.BAR_MINUTES, TimeFrameUnit.Minute)


def get_tradable_symbols(asset_class: str) -> list:
    """The bot's real addressable universe, fetched live from Alpaca --
    not a hardcoded watchlist. Stocks are filtered to major listed
    exchanges (excludes OTC/pink-sheet names, which are thin enough that
    15-min bars are mostly noise, not signal). Crypto gets no exchange
    filter: Alpaca's full tradable crypto list is used as-is.
    """
    if asset_class == "crypto":
        req = GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
        assets = trading_client.get_all_assets(req)
        return sorted(a.symbol for a in assets if a.tradable)
    req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    out = []
    for a in assets:
        if not a.tradable:
            continue
        exch = getattr(a.exchange, "value", str(a.exchange))
        if exch in config.STOCK_EXCHANGE_ALLOWLIST:
            out.append(a.symbol)
    return sorted(out)


def get_bars_batch(symbols: list, asset_class: str, bars: int = config.LOOKBACK_BARS) -> dict:
    """Fetch bars for many symbols via chunked multi-symbol requests instead
    of one API call per symbol -- the only way this stays fast at
    full-universe scale (thousands of stocks). Returns {symbol: [bars...]}
    in chronological (oldest-first) order, matching what strategy.py's
    indicator math expects (bars[-1] is the latest).

    Deliberately omits `limit`: for a MULTI-symbol request, Alpaca applies
    `limit` to the total record count across the whole combined response,
    not per symbol (unlike a single-symbol request, where it's per-symbol).
    Confirmed live: requesting 73 crypto symbols with limit=80 returned bar
    data for exactly 1 symbol -- one symbol's ~80 bars consumed the entire
    global budget before the rest were ever included. Bounding by `start`
    and trimming to the most recent `bars` per symbol client-side avoids
    that trap entirely.
    """
    # Crypto trades 24/7 with no session gaps, so a short buffer covers it;
    # stocks need enough slack to span a weekend/holiday close.
    buffer_multiplier = 1.5 if asset_class == "crypto" else 6
    start = datetime.now() - timedelta(minutes=config.BAR_MINUTES * bars * buffer_multiplier)
    result = {}
    chunk_size = config.BAR_FETCH_CHUNK_SIZE
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        try:
            if asset_class == "crypto":
                request = CryptoBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=BAR_TIMEFRAME,
                    start=start,
                    sort=Sort.ASC,
                )
                barset = crypto_data_client.get_crypto_bars(request)
            else:
                request = StockBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=BAR_TIMEFRAME,
                    start=start,
                    sort=Sort.ASC,
                )
                barset = stock_data_client.get_stock_bars(request)
        except Exception:
            continue  # one bad chunk shouldn't sink the whole cycle
        for sym in chunk:
            try:
                sym_bars = list(barset[sym])
            except KeyError:
                continue
            if sym_bars:
                # ASC order already matches the chronological order the
                # indicator math expects; trim to the most recent `bars`.
                result[sym] = sym_bars[-bars:]
        time.sleep(config.BAR_FETCH_CHUNK_DELAY)
    return result


def place_market_order(symbol: str, qty: float, side: str, asset_class: str = "stock"):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    # Alpaca requires GTC (not DAY) time-in-force for crypto orders.
    tif = TimeInForce.GTC if asset_class == "crypto" else TimeInForce.DAY
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=tif,
    )
    return trading_client.submit_order(req)


def close_position(symbol: str):
    return trading_client.close_position(symbol)


def get_order(order_id):
    return trading_client.get_order_by_id(order_id)


def wait_for_fill(order_id, timeout: float = 8, poll_interval: float = 0.5):
    """Poll until the order has a filled_avg_price, or timeout. Returns the
    latest order object either way (falls back to unfilled if it times out)."""
    elapsed = 0.0
    order = get_order(order_id)
    while order.filled_avg_price is None and elapsed < timeout:
        time.sleep(poll_interval)
        elapsed += poll_interval
        order = get_order(order_id)
    return order


def is_market_open():
    clock = trading_client.get_clock()
    return clock.is_open
