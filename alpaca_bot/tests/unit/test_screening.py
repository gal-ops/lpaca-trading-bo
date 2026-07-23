"""Unit tests for the universe screening/ranking pipeline (spec section 6)
and the cheap liquidity features it depends on."""

from datetime import datetime, timedelta, timezone

from alpaca_bot.config import load_strategy_config
from alpaca_bot.data.bars import Bar
from alpaca_bot.features.liquidity import atr_pct, average_volume, median_daily_dollar_volume
from alpaca_bot.universe.discovery import AssetRecord
from alpaca_bot.universe.screening import rank_candidates, screen_crypto_universe, screen_equity_universe


def _asset(symbol) -> AssetRecord:
    return AssetRecord(
        asset_id=f"id-{symbol}", symbol=symbol, name=symbol, asset_class="us_equity",
        exchange="NASDAQ", status="active", tradable=True, fractionable=True,
        marginable=True, shortable=True, easy_to_borrow=True,
        maintenance_margin_requirement=0.25, min_order_increment=1,
        min_trade_increment=1, price_increment=0.01, last_checked=datetime.now(timezone.utc),
    )


def _bars(closes: list[float], volume: float = 2_000_000) -> list[Bar]:
    base = datetime.now(timezone.utc) - timedelta(days=len(closes))
    bars = []
    for i, close in enumerate(closes):
        bars.append(Bar(
            ts=base + timedelta(days=i), open=close, high=close * 1.01,
            low=close * 0.99, close=close, volume=volume,
        ))
    return bars


def test_median_dollar_volume_and_average_volume():
    bars = _bars([100.0] * 20, volume=1_000_000)
    assert median_daily_dollar_volume(bars) == 100_000_000
    assert average_volume(bars) == 1_000_000


def test_atr_pct_needs_enough_history():
    short_bars = _bars([100.0] * 5)
    assert atr_pct(short_bars, period=14) == 0.0

    varying = [100 + (i % 3) for i in range(30)]
    bars = _bars(varying)
    result = atr_pct(bars, period=14)
    assert 0 < result < 0.10


def test_screen_equity_universe_excludes_below_price_floor():
    cfg = load_strategy_config()
    asset = _asset("PENNY")
    bars = _bars([2.0] * 30, volume=5_000_000)
    results = screen_equity_universe([asset], {"PENNY": bars}, cfg)
    assert results[0].eligible is False
    assert "below minimum" in results[0].reason


def test_screen_equity_universe_excludes_thin_liquidity():
    cfg = load_strategy_config()
    asset = _asset("THIN")
    bars = _bars([50.0] * 30, volume=1_000)  # tiny dollar volume
    results = screen_equity_universe([asset], {"THIN": bars}, cfg)
    assert results[0].eligible is False
    assert "dollar volume" in results[0].reason


def test_screen_equity_universe_excludes_missing_bar_data():
    cfg = load_strategy_config()
    asset = _asset("NODATA")
    results = screen_equity_universe([asset], {}, cfg)
    assert results[0].eligible is False
    assert results[0].reason == "no bar data available"


def test_screen_equity_universe_passes_healthy_liquid_stock():
    cfg = load_strategy_config()
    asset = _asset("AAPL")
    varying = [190 + (i % 5) for i in range(60)]
    bars = _bars(varying, volume=50_000_000)
    results = screen_equity_universe([asset], {"AAPL": bars}, cfg)
    assert results[0].eligible is True
    assert results[0].reason is None
    assert results[0].features["median_dollar_volume"] > 0


def test_screen_crypto_universe_excludes_thin_volume():
    asset = _asset("XYZ/USD")
    bars = _bars([1.0] * 30, volume=10)
    results = screen_crypto_universe([asset], {"XYZ/USD": bars}, min_median_dollar_volume=1_000_000)
    assert results[0].eligible is False


def test_rank_candidates_orders_by_liquidity_and_volume():
    cfg = load_strategy_config()
    high = _asset("HIGH")
    low = _asset("LOW")
    high_bars = _bars([100.0] * 60, volume=50_000_000)
    low_bars = _bars([100.0] * 60, volume=2_000_000)
    results = screen_equity_universe([high, low], {"HIGH": high_bars, "LOW": low_bars}, cfg)
    ranked = rank_candidates(results)
    assert [r.symbol for r in ranked] == ["HIGH", "LOW"]


def test_rank_candidates_respects_top_n_and_excludes_ineligible():
    cfg = load_strategy_config()
    good = _asset("GOOD")
    bad = _asset("BAD")
    good_bars = _bars([100.0] * 60, volume=50_000_000)
    bad_bars = _bars([2.0] * 60, volume=50_000_000)  # fails price floor
    results = screen_equity_universe([good, bad], {"GOOD": good_bars, "BAD": bad_bars}, cfg)
    ranked = rank_candidates(results, top_n=5)
    assert [r.symbol for r in ranked] == ["GOOD"]
