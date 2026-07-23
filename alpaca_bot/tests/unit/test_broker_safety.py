"""Unit tests for the paper-only safety gate (spec section 1, rules 2-7).
No real Alpaca credentials are used or required -- everything network-
facing is mocked."""

from unittest.mock import MagicMock, patch

import pytest

from alpaca_bot.broker.client import BrokerClient, PaperTradingSafetyError
from alpaca_bot.config import Settings


def _settings(**overrides) -> Settings:
    s = Settings.__new__(Settings)  # bypass __init__'s load_dotenv/env reads
    s.api_key = overrides.get("api_key", "test-key")
    s.secret_key = overrides.get("secret_key", "test-secret")
    s.base_url = overrides.get("base_url", "https://paper-api.alpaca.markets")
    s.paper_trading_flag = overrides.get("paper_trading_flag", "true")
    s.database_path = overrides.get("database_path", "data/state/test.db")
    return s


@patch("alpaca_bot.broker.client.CryptoHistoricalDataClient")
@patch("alpaca_bot.broker.client.StockHistoricalDataClient")
@patch("alpaca_bot.broker.client.TradingClient")
def test_construction_succeeds_with_valid_paper_settings(mock_trading_client, *_):
    broker = BrokerClient(_settings())
    assert broker.trading_client is not None
    mock_trading_client.assert_called_once()
    _, kwargs = mock_trading_client.call_args
    assert kwargs["paper"] is True


def test_refuses_non_paper_base_url():
    with pytest.raises(PaperTradingSafetyError, match="not an Alpaca paper endpoint"):
        BrokerClient(_settings(base_url="https://api.alpaca.markets"))


@pytest.mark.parametrize("flag", ["True", "TRUE", "1", "yes", "", "false"])
def test_refuses_unless_paper_trading_flag_is_exactly_true(flag):
    with pytest.raises(PaperTradingSafetyError, match="PAPER_TRADING must be exactly"):
        BrokerClient(_settings(paper_trading_flag=flag))


def test_refuses_missing_credentials():
    with pytest.raises(PaperTradingSafetyError, match="ALPACA_API_KEY"):
        BrokerClient(_settings(api_key=None, secret_key=None))


@patch("alpaca_bot.broker.client.CryptoHistoricalDataClient")
@patch("alpaca_bot.broker.client.StockHistoricalDataClient")
@patch("alpaca_bot.broker.client.TradingClient")
def test_refuses_inactive_account(mock_trading_client, *_):
    account = MagicMock(account_number="PA123", status="ACCOUNT_STATUS.INACTIVE",
                         trading_blocked=False)
    mock_trading_client.return_value.get_account.return_value = account
    broker = BrokerClient(_settings())
    with pytest.raises(PaperTradingSafetyError, match="not ACTIVE"):
        broker.verify_account_safe_to_trade()


@patch("alpaca_bot.broker.client.CryptoHistoricalDataClient")
@patch("alpaca_bot.broker.client.StockHistoricalDataClient")
@patch("alpaca_bot.broker.client.TradingClient")
def test_refuses_trading_blocked_account(mock_trading_client, *_):
    account = MagicMock(account_number="PA123", status="ACTIVE", trading_blocked=True)
    mock_trading_client.return_value.get_account.return_value = account
    broker = BrokerClient(_settings())
    with pytest.raises(PaperTradingSafetyError, match="trading_blocked"):
        broker.verify_account_safe_to_trade()


@patch("alpaca_bot.broker.client.CryptoHistoricalDataClient")
@patch("alpaca_bot.broker.client.StockHistoricalDataClient")
@patch("alpaca_bot.broker.client.TradingClient")
def test_refuses_when_market_data_auth_fails(mock_trading_client, *_):
    account = MagicMock(account_number="PA123", status="ACTIVE", trading_blocked=False)
    mock_trading_client.return_value.get_account.return_value = account
    mock_trading_client.return_value.get_all_assets.side_effect = Exception("401 Unauthorized")
    broker = BrokerClient(_settings())
    with pytest.raises(PaperTradingSafetyError, match="Market-data authentication failed"):
        broker.verify_account_safe_to_trade()


@patch("alpaca_bot.broker.client.CryptoHistoricalDataClient")
@patch("alpaca_bot.broker.client.StockHistoricalDataClient")
@patch("alpaca_bot.broker.client.TradingClient")
def test_succeeds_end_to_end_with_healthy_paper_account(mock_trading_client, *_):
    account = MagicMock(
        id="acct-1", account_number="PA123", status="ACTIVE", trading_blocked=False,
        equity="540.00", cash="540.00", buying_power="540.00", pattern_day_trader=False,
    )
    mock_trading_client.return_value.get_account.return_value = account
    mock_trading_client.return_value.get_all_assets.return_value = [MagicMock()]
    broker = BrokerClient(_settings())
    snapshot = broker.verify_account_safe_to_trade()
    assert snapshot.status == "ACTIVE"
    assert snapshot.equity == 540.0


def test_settings_repr_never_includes_secrets():
    s = _settings(api_key="SECRET_KEY_VALUE", secret_key="SECRET_SECRET_VALUE")
    assert "SECRET_KEY_VALUE" not in repr(s)
    assert "SECRET_SECRET_VALUE" not in repr(s)
