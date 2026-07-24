"""Real Alpaca paper-endpoint connectivity test. Marked `integration` so
it is excluded from the default `pytest` run (see pyproject.toml's
addopts) and never runs accidentally without credentials. Run explicitly
with: pytest -m integration tests/integration
"""

import os

import pytest

from alpaca_bot.broker.client import BrokerClient
from alpaca_bot.config import get_settings


@pytest.mark.integration
def test_real_paper_account_connection():
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        pytest.skip("ALPACA_API_KEY/ALPACA_SECRET_KEY not set -- skipping real API test")

    settings = get_settings()
    broker = BrokerClient(settings)
    account = broker.verify_account_safe_to_trade()

    assert account.status == "AccountStatus.ACTIVE" or "ACTIVE" in account.status
    assert account.equity >= 0
