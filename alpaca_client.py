from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
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


def get_bars(symbol: str, asset_class: str, bars: int = config.LOOKBACK_BARS):
    """asset_class is 'stock' or 'crypto'."""
    start = datetime.now() - timedelta(days=bars * 2)
    if asset_class == "crypto":
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
            limit=bars,
        )
        barset = crypto_data_client.get_crypto_bars(request)
    else:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Hour,
            start=start,
            limit=bars,
        )
        barset = stock_data_client.get_stock_bars(request)
    return barset[symbol]


def place_market_order(symbol: str, qty: float, side: str):
    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
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
