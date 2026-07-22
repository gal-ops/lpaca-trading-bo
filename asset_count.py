"""Read-only: report how many tradable assets Alpaca actually offers, by class."""
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
import config

tc = TradingClient(config.API_KEY, config.SECRET_KEY, paper=True)

stocks = tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
tradable_stocks = [a for a in stocks if a.tradable]
print(f"US equities: active={len(stocks)} tradable={len(tradable_stocks)}")

crypto = tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE))
tradable_crypto = [a for a in crypto if a.tradable]
print(f"Crypto pairs: active={len(crypto)} tradable={len(tradable_crypto)}")
print("Crypto symbols:", ", ".join(sorted(a.symbol for a in tradable_crypto)))
