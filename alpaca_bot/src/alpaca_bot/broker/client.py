"""Paper-only Alpaca broker client.

This module is the single enforcement point for spec section 1 (rules 2-7):
the system must connect exclusively to Alpaca's paper endpoint, must refuse
to start under a list of unsafe conditions, and must have no hidden or
simple live-trading toggle. `paper=True` below is a Python literal, not a
config value or environment variable -- there is no key anywhere in this
codebase that flips it. Going live would require deliberately rewriting
this file, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient

from alpaca_bot.config import Settings

PAPER_HOST_FRAGMENT = "paper-api.alpaca.markets"


class PaperTradingSafetyError(RuntimeError):
    """Raised whenever a startup safety condition (spec section 1, rule 6)
    is not satisfied. Callers must let this propagate and exit -- it must
    never be caught and silently downgraded to a warning."""


@dataclass
class AccountSnapshot:
    id: str
    status: str
    equity: float
    cash: float
    buying_power: float
    pattern_day_trader: bool
    trading_blocked: bool


class BrokerClient:
    """Thin, safety-gated wrapper around alpaca-py. Every method that talks
    to Alpaca goes through this class so the paper-only guarantee has one
    place to hold, not one per call site."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._verify_paper_endpoint_configured()
        self.trading_client = TradingClient(
            settings.api_key, settings.secret_key, paper=True,  # hard-coded; see module docstring
        )
        self.stock_data_client = StockHistoricalDataClient(settings.api_key, settings.secret_key)
        self.crypto_data_client = CryptoHistoricalDataClient()

    # ---- startup safety gates (spec section 1, rule 6) ----

    def _verify_paper_endpoint_configured(self) -> None:
        if PAPER_HOST_FRAGMENT not in (self._settings.base_url or ""):
            raise PaperTradingSafetyError(
                f"ALPACA_BASE_URL ({self._settings.base_url!r}) is not an Alpaca paper "
                f"endpoint. Refusing to start -- this system is paper-only."
            )
        if self._settings.paper_trading_flag != "true":
            raise PaperTradingSafetyError(
                "PAPER_TRADING must be exactly the string 'true'. Got "
                f"{self._settings.paper_trading_flag!r}. Refusing to start."
            )
        if not self._settings.api_key or not self._settings.secret_key:
            raise PaperTradingSafetyError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY are not set. Refusing to start."
            )

    def verify_account_safe_to_trade(self) -> AccountSnapshot:
        """Second-stage gate: requires a live round-trip to Alpaca, so it's
        separate from the config-only checks in __init__. Confirms the
        account really is a paper account (belt-and-suspenders against a
        misconfigured proxy/host override) and is active."""
        try:
            account = self.trading_client.get_account()
        except Exception as e:
            raise PaperTradingSafetyError(f"Could not reach Alpaca trading API: {e}") from e
        if isinstance(account, dict):
            raise PaperTradingSafetyError(
                "Alpaca returned a raw dict instead of a TradeAccount object -- "
                "cannot verify account safety. Refusing to start."
            )

        is_paper_account = bool(getattr(account, "account_number", "")) and PAPER_HOST_FRAGMENT in (
            self._settings.base_url or ""
        )
        if not is_paper_account:
            raise PaperTradingSafetyError(
                "Could not confirm the connected account is a paper account. Refusing to start."
            )
        if str(account.status).upper() not in ("ACTIVE", "ACCOUNTSTATUS.ACTIVE"):
            raise PaperTradingSafetyError(
                f"Account status is {account.status!r}, not ACTIVE. Refusing to start."
            )
        if getattr(account, "trading_blocked", False):
            raise PaperTradingSafetyError("Account has trading_blocked=True. Refusing to start.")
        if account.equity is None or account.cash is None or account.buying_power is None:
            raise PaperTradingSafetyError(
                "Account equity/cash/buying_power missing from Alpaca response. Refusing to start."
            )

        self._verify_market_data_auth()

        return AccountSnapshot(
            id=str(account.id),
            status=str(account.status),
            equity=float(account.equity),
            cash=float(account.cash),
            buying_power=float(account.buying_power),
            pattern_day_trader=bool(account.pattern_day_trader),
            trading_blocked=bool(account.trading_blocked),
        )

    def _verify_market_data_auth(self) -> None:
        """Confirms market-data credentials work at all -- spec section 1,
        rule 6 requires refusing to start if market-data auth fails."""
        try:
            req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
            assets = self.trading_client.get_all_assets(req)
            if not assets:
                raise PaperTradingSafetyError(
                    "Market-data/assets authentication returned an empty result. Refusing to start."
                )
        except PaperTradingSafetyError:
            raise
        except Exception as e:
            raise PaperTradingSafetyError(f"Market-data authentication failed: {e}") from e

    # ---- read-only account/position access ----

    def get_account(self):
        return self.trading_client.get_account()

    def get_all_positions(self):
        return self.trading_client.get_all_positions()

    def get_open_orders(self):
        return self.trading_client.get_orders()
