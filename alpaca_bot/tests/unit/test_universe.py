"""Unit tests for dynamic universe discovery (spec section 2). All Alpaca
calls are mocked -- no real credentials or network access needed."""

from dataclasses import replace
from unittest.mock import MagicMock

from alpaca_bot.universe.discovery import (
    AssetRecord,
    discover_crypto_universe,
    discover_equity_universe,
    is_shortable_now,
    select_preferred_crypto_pairs,
    tradable_universe,
)


def _mock_asset(symbol, tradable=True, status="active", shortable=True,
                 easy_to_borrow=True, fractionable=True, marginable=True,
                 asset_class="us_equity", exchange="NASDAQ"):
    a = MagicMock()
    a.id = f"id-{symbol}"
    a.symbol = symbol
    a.name = f"{symbol} Inc."
    a.asset_class = asset_class
    a.exchange = exchange
    a.status = status
    a.tradable = tradable
    a.fractionable = fractionable
    a.marginable = marginable
    a.shortable = shortable
    a.easy_to_borrow = easy_to_borrow
    a.maintenance_margin_requirement = 0.25
    a.min_order_size = 1
    a.min_trade_increment = 1
    a.price_increment = 0.01
    return a


def _mock_broker(assets):
    broker = MagicMock()
    broker.trading_client.get_all_assets.return_value = assets
    return broker


def test_discover_equity_universe_returns_every_asset():
    assets = [_mock_asset("AAPL"), _mock_asset("XYZ", tradable=False)]
    broker = _mock_broker(assets)
    discovered = discover_equity_universe(broker)
    assert len(discovered) == 2
    assert {a.symbol for a in discovered} == {"AAPL", "XYZ"}


def test_tradable_universe_filters_non_tradable_and_inactive():
    assets = [
        _mock_asset("AAPL", tradable=True, status="active"),
        _mock_asset("HALTED", tradable=False, status="active"),
        _mock_asset("DELISTED", tradable=True, status="inactive"),
    ]
    discovered = [discover_equity_universe(_mock_broker([a]))[0] for a in assets]
    tradable = tradable_universe(discovered)
    assert [a.symbol for a in tradable] == ["AAPL"]


def test_is_shortable_now_requires_all_three_flags():
    ok = AssetRecord(
        asset_id="1", symbol="AAPL", name="Apple", asset_class="us_equity",
        exchange="NASDAQ", status="active", tradable=True, fractionable=True,
        marginable=True, shortable=True, easy_to_borrow=True,
        maintenance_margin_requirement=0.25, min_order_increment=1,
        min_trade_increment=1, price_increment=0.01, last_checked=None,
    )
    assert is_shortable_now(ok) is True

    for field in ("tradable", "shortable", "easy_to_borrow"):
        bad = replace(ok, **{field: False})
        assert is_shortable_now(bad) is False


def test_discover_crypto_universe_returns_every_asset():
    assets = [_mock_asset("BTC/USD", asset_class="crypto", exchange=None)]
    broker = _mock_broker(assets)
    discovered = discover_crypto_universe(broker)
    assert len(discovered) == 1
    assert discovered[0].symbol == "BTC/USD"


def test_select_preferred_crypto_pairs_dedupes_by_base_asset():
    assets = [
        _mock_asset("BTC/USD", asset_class="crypto"),
        _mock_asset("BTC/USDT", asset_class="crypto"),
        _mock_asset("ETH/USDC", asset_class="crypto"),
        _mock_asset("ETH/BTC", asset_class="crypto"),  # BTC-quoted, excluded
    ]
    discovered = [discover_crypto_universe(_mock_broker([a]))[0] for a in assets]
    selected = select_preferred_crypto_pairs(discovered)
    symbols = {a.symbol for a in selected}
    # BTC's only USD-preference pair is BTC/USD; ETH has no USD/USDT pair,
    # so its best available is ETH/USDC. ETH/BTC (BTC-quoted) must not appear.
    assert symbols == {"BTC/USD", "ETH/USDC"}


def test_select_preferred_crypto_pairs_prefers_usd_over_usdc_and_usdt():
    assets = [
        _mock_asset("SOL/USD", asset_class="crypto"),
        _mock_asset("SOL/USDC", asset_class="crypto"),
        _mock_asset("SOL/USDT", asset_class="crypto"),
    ]
    discovered = [discover_crypto_universe(_mock_broker([a]))[0] for a in assets]
    selected = select_preferred_crypto_pairs(discovered)
    assert len(selected) == 1
    assert selected[0].symbol == "SOL/USD"
