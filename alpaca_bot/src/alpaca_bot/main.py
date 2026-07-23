"""Entry point. Currently exercises phases 1-3 (scaffold/config, paper-only
broker, asset discovery, SQLite persistence) -- later phases (screening,
regimes, strategies, models, execution, reporting) plug in here as they're
built. Does not place any orders."""

from __future__ import annotations

import logging
import sys

from alpaca_bot.broker.client import BrokerClient, PaperTradingSafetyError
from alpaca_bot.config import get_settings, load_strategy_config
from alpaca_bot.persistence.db import Database
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

    db = Database(settings.database_path)

    # Spec section 1, rule 6: refuse to start if the previous run has
    # unreconciled orders or positions. Real reconciliation against the
    # broker's live order/position state (not just clearing the flag)
    # belongs to phase 10 (execution/reconciliation) -- for now this can
    # only detect and refuse, not repair.
    unreconciled, reason = db.has_unreconciled_state()
    if unreconciled:
        log.error("Refusing to start: unreconciled state from a previous run -- %s", reason)
        db.close()
        return 1

    equity_discovered = discover_equity_universe(broker)
    equity_tradable = tradable_universe(equity_discovered)
    crypto_discovered = discover_crypto_universe(broker)
    crypto_tradable = tradable_universe(crypto_discovered)
    crypto_selected = select_preferred_crypto_pairs(
        crypto_tradable, cfg["universe"]["crypto"]["quote_currency_preference"]
    )
    db.upsert_assets(equity_discovered)
    db.upsert_assets(crypto_discovered)

    log.info(
        "Equity universe: discovered=%d tradable=%d",
        len(equity_discovered), len(equity_tradable),
    )
    log.info(
        "Crypto universe: discovered=%d tradable=%d selected(dedup)=%d",
        len(crypto_discovered), len(crypto_tradable), len(crypto_selected),
    )
    db.record_pnl_snapshot({
        "equity": account.equity, "cash": account.cash,
        "open_positions": len(broker.get_all_positions()),
    })
    log.info("Phases 4-12 (screening, regimes, strategies, models, risk validator, "
             "execution, reporting, tests) are not yet wired in -- no orders "
             "will be placed by this entry point.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
