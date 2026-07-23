"""Entry point. Currently exercises phases 1-2 only (scaffold/config,
paper-only broker, asset discovery) -- later phases (persistence,
screening, regimes, strategies, models, execution, reporting) plug in
here as they're built. Does not place any orders."""

from __future__ import annotations

import logging
import sys

from alpaca_bot.broker.client import BrokerClient, PaperTradingSafetyError
from alpaca_bot.config import get_settings, load_strategy_config
from alpaca_bot.universe.discovery import (
    discover_crypto_universe,
    discover_equity_universe,
    select_preferred_crypto_pairs,
    tradable_universe,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("alpaca_bot")


def run() -> int:
    settings = get_settings()
    cfg = load_strategy_config()
    log.info("Settings: %s", settings)

    try:
        broker = BrokerClient(settings)
        account = broker.verify_account_safe_to_trade()
    except PaperTradingSafetyError as e:
        log.error("Refusing to start: %s", e)
        return 1

    log.info(
        "Paper account OK: id=%s status=%s equity=$%.2f cash=$%.2f",
        account.id, account.status, account.equity, account.cash,
    )

    equity_discovered = discover_equity_universe(broker)
    equity_tradable = tradable_universe(equity_discovered)
    crypto_discovered = discover_crypto_universe(broker)
    crypto_tradable = tradable_universe(crypto_discovered)
    crypto_selected = select_preferred_crypto_pairs(
        crypto_tradable, cfg["universe"]["crypto"]["quote_currency_preference"]
    )

    log.info(
        "Equity universe: discovered=%d tradable=%d",
        len(equity_discovered), len(equity_tradable),
    )
    log.info(
        "Crypto universe: discovered=%d tradable=%d selected(dedup)=%d",
        len(crypto_discovered), len(crypto_tradable), len(crypto_selected),
    )
    log.info("Phases 3-12 (persistence, screening, regimes, strategies, models, "
             "execution, reporting, tests) are not yet wired in -- no orders "
             "will be placed by this entry point.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
