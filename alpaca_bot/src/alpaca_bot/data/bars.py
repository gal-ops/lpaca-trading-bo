"""Batched historical bar fetching (spec section 6, step 2: batch-fetch
daily and recent intraday bars). Chunked multi-symbol requests, not one
call per symbol -- the only way this stays fast across the full
tradable universe (~13k equities + dozens of crypto pairs)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpaca.common.enums import Sort
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from alpaca_bot.broker.client import BrokerClient

CHUNK_SIZE = 100
CHUNK_DELAY_SECONDS = 0.3


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


def _to_bar(raw) -> Bar:
    ts = raw.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Bar(
        ts=ts, open=float(raw.open), high=float(raw.high), low=float(raw.low),
        close=float(raw.close), volume=float(raw.volume),
        vwap=float(raw.vwap) if getattr(raw, "vwap", None) is not None else None,
    )


def get_daily_bars_batch(
    broker: BrokerClient,
    symbols: list[str],
    asset_class: str,
    lookback_days: int = 90,
) -> dict[str, list[Bar]]:
    """Returns {symbol: [Bar, ...]} in chronological order, oldest first.
    Skips a chunk on any request error rather than failing the whole
    screening pass -- one bad chunk should not sink the cycle."""
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    timeframe = TimeFrame(1, TimeFrameUnit.Day)
    result: dict[str, list[Bar]] = {}

    for i in range(0, len(symbols), CHUNK_SIZE):
        chunk = symbols[i:i + CHUNK_SIZE]
        try:
            if asset_class == "crypto":
                crypto_req = CryptoBarsRequest(symbol_or_symbols=chunk, timeframe=timeframe,
                                                 start=start, sort=Sort.ASC)
                barset = broker.crypto_data_client.get_crypto_bars(crypto_req)
            else:
                stock_req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=timeframe,
                                               start=start, sort=Sort.ASC)
                barset = broker.stock_data_client.get_stock_bars(stock_req)
        except Exception:
            continue
        for sym in chunk:
            try:
                raw_bars = list(barset[sym])
            except KeyError:
                continue
            if raw_bars:
                result[sym] = [_to_bar(b) for b in raw_bars]
        time.sleep(CHUNK_DELAY_SECONDS)
    return result


def get_intraday_bars_batch(
    broker: BrokerClient,
    symbols: list[str],
    asset_class: str,
    minutes: int,
    lookback_bars: int,
) -> dict[str, list[Bar]]:
    """Same batching strategy as get_daily_bars_batch but for sub-daily
    timeframes (1/5/15-minute, hourly), used by regime engines and
    strategies once a symbol has been shortlisted."""
    buffer_multiplier = 1.5 if asset_class == "crypto" else 6
    start = datetime.now(timezone.utc) - timedelta(minutes=minutes * lookback_bars * buffer_multiplier)
    timeframe = TimeFrame(minutes, TimeFrameUnit.Minute)
    result: dict[str, list[Bar]] = {}

    for i in range(0, len(symbols), CHUNK_SIZE):
        chunk = symbols[i:i + CHUNK_SIZE]
        try:
            if asset_class == "crypto":
                crypto_req = CryptoBarsRequest(symbol_or_symbols=chunk, timeframe=timeframe,
                                                 start=start, sort=Sort.ASC)
                barset = broker.crypto_data_client.get_crypto_bars(crypto_req)
            else:
                stock_req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=timeframe,
                                               start=start, sort=Sort.ASC)
                barset = broker.stock_data_client.get_stock_bars(stock_req)
        except Exception:
            continue
        for sym in chunk:
            try:
                raw_bars = list(barset[sym])
            except KeyError:
                continue
            if raw_bars:
                result[sym] = [_to_bar(b) for b in raw_bars[-lookback_bars:]]
        time.sleep(CHUNK_DELAY_SECONDS)
    return result
