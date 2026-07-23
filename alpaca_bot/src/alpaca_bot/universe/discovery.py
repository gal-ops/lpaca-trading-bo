"""Dynamic Alpaca asset universe discovery (spec section 2).

Never hardcode a ticker list. `discover_equity_universe`/`discover_crypto_universe`
hit Alpaca's live Assets API and return every currently active asset Alpaca
reports -- the `discovered_universe`. Filtering down to `tradable_universe`
(status == active, tradable == true, correct class) is a separate, cheap
step so illiquid/non-tradable symbols are never silently deleted from
discovery, only excluded from a given cycle with a recorded reason.

`eligible_now` (the third layer: passes live liquidity/data-quality/spread/
volatility/risk/session checks) belongs to the universe *screening*
pipeline (spec section 6), not discovery, since it needs live bars/quotes --
that's built in the universe-screening phase, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from alpaca_bot.broker.client import BrokerClient


@dataclass
class AssetRecord:
    """Every field the spec asks to persist per asset (section 2), plus a
    last-checked timestamp. `exclusion_reason` is set by later screening
    steps when an asset that's real and tradable is excluded from a
    particular cycle -- it must never be dropped from discovery entirely."""

    asset_id: str
    symbol: str
    name: str | None
    asset_class: str
    exchange: str | None
    status: str
    tradable: bool
    fractionable: bool
    marginable: bool
    shortable: bool
    easy_to_borrow: bool
    maintenance_margin_requirement: float | None
    min_order_increment: float | None
    min_trade_increment: float | None
    price_increment: float | None
    last_checked: datetime
    exclusion_reason: str | None = field(default=None)


def _to_asset_record(asset) -> AssetRecord:
    exch = getattr(asset, "exchange", None)
    exch_str = getattr(exch, "value", str(exch)) if exch is not None else None
    asset_class = getattr(asset, "asset_class", None)
    asset_class_str = getattr(asset_class, "value", str(asset_class)) if asset_class is not None else ""
    status = getattr(asset, "status", None)
    status_str = getattr(status, "value", str(status)) if status is not None else ""

    return AssetRecord(
        asset_id=str(asset.id),
        symbol=asset.symbol,
        name=getattr(asset, "name", None),
        asset_class=asset_class_str,
        exchange=exch_str,
        status=status_str,
        tradable=bool(getattr(asset, "tradable", False)),
        fractionable=bool(getattr(asset, "fractionable", False)),
        marginable=bool(getattr(asset, "marginable", False)),
        shortable=bool(getattr(asset, "shortable", False)),
        easy_to_borrow=bool(getattr(asset, "easy_to_borrow", False)),
        maintenance_margin_requirement=getattr(asset, "maintenance_margin_requirement", None),
        min_order_increment=getattr(asset, "min_order_size", None),
        min_trade_increment=getattr(asset, "min_trade_increment", None),
        price_increment=getattr(asset, "price_increment", None),
        last_checked=datetime.now(timezone.utc),
    )


def discover_equity_universe(broker: BrokerClient) -> list[AssetRecord]:
    """The full `discovered_universe` for US equities/ETFs -- every active
    asset Alpaca's Assets API returns, not filtered by tradability yet."""
    req = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = broker.trading_client.get_all_assets(req)
    return [_to_asset_record(a) for a in assets]


def discover_crypto_universe(broker: BrokerClient) -> list[AssetRecord]:
    """The full `discovered_universe` for crypto pairs."""
    req = GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE)
    assets = broker.trading_client.get_all_assets(req)
    return [_to_asset_record(a) for a in assets]


def tradable_universe(discovered: list[AssetRecord]) -> list[AssetRecord]:
    """Layer 2: status == active, tradable == true. Asset-class correctness
    is already guaranteed by which discover_* function produced the list."""
    return [a for a in discovered if a.tradable and a.status.upper() == "ACTIVE"]


def is_shortable_now(asset: AssetRecord) -> bool:
    """Spec section 2: an equity short is permitted only when tradable AND
    shortable AND easy_to_borrow are all true. If any field is missing or
    uncertain, reject -- hence the strict `is True` (not truthy) checks,
    since `None` must never silently pass as "shortable"."""
    return asset.tradable is True and asset.shortable is True and asset.easy_to_borrow is True


def _base_symbol(pair_symbol: str) -> str:
    """'BTC/USD' -> 'BTC'. Crypto symbols are always BASE/QUOTE."""
    return pair_symbol.split("/")[0]


def select_preferred_crypto_pairs(
    crypto_assets: list[AssetRecord],
    quote_preference: list[str] | None = None,
) -> list[AssetRecord]:
    """Prevents duplicate exposure to the same base asset through several
    quote pairs (spec section 2): for each base asset, keep only the pair
    quoted in the most-preferred available currency (USD, then USDC, then
    USDT by default). BTC-quoted pairs (e.g. ETH/BTC) are excluded here --
    the spec requires risk accounting/conversion to be implemented
    correctly before those are used at all, which is a later-phase concern."""
    quote_preference = quote_preference or ["USD", "USDC", "USDT"]
    by_base: dict[str, dict[str, AssetRecord]] = {}
    for asset in crypto_assets:
        if "/" not in asset.symbol:
            continue
        base, quote = asset.symbol.split("/", 1)
        if quote not in quote_preference:
            continue  # e.g. BTC-quoted pairs -- excluded until conversion is implemented
        by_base.setdefault(base, {})[quote] = asset

    selected = []
    for base, by_quote in by_base.items():
        for quote in quote_preference:
            if quote in by_quote:
                selected.append(by_quote[quote])
                break
    return selected
