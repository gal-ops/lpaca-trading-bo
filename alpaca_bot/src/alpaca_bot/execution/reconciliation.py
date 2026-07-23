"""Broker-state reconciliation (spec section 10: "reconcile broker state
after every order event"; spec section 1: refuse to start with
unreconciled state; spec section 11: "REST and WebSocket positions
disagree" is itself a kill-switch condition).

The broker is always the source of truth. This module never assumes the
local database is right when it disagrees with Alpaca -- it corrects the
database to match, and reports every correction it had to make so the
caller can decide whether the drift was benign (a normal fill we now know
about) or alarming (something the bot didn't expect at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alpaca_bot.broker.client import BrokerClient
from alpaca_bot.persistence.db import Database


@dataclass
class ReconciliationResult:
    consistent: bool
    discrepancies: list[str] = field(default_factory=list)


def reconcile(broker: BrokerClient, db: Database) -> ReconciliationResult:
    discrepancies: list[str] = []

    broker_positions = {p.symbol: p for p in broker.get_all_positions()}
    db_positions = {row["symbol"]: row for row in db.open_positions()}

    for symbol, pos in broker_positions.items():
        if symbol not in db_positions:
            discrepancies.append(f"position {symbol} exists at broker but not in local DB -- importing it")
            db.upsert_position({
                "symbol": symbol,
                "asset_class": "crypto" if ("/" in symbol or symbol.endswith("USD")) else "stock",
                "qty": float(pos.qty), "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(getattr(pos, "current_price", 0) or 0),
                "market_value": float(getattr(pos, "market_value", 0) or 0),
                "unrealized_pl": float(getattr(pos, "unrealized_pl", 0) or 0),
                "unrealized_plpc": float(getattr(pos, "unrealized_plpc", 0) or 0),
            })

    for symbol in db_positions:
        if symbol not in broker_positions:
            discrepancies.append(f"position {symbol} tracked locally but no longer exists at broker -- removing")
            db.remove_position(symbol)

    broker_open_order_ids = {str(o.id) for o in broker.get_open_orders()}
    for row in db.open_orders():
        broker_order_id = row["broker_order_id"]
        if broker_order_id and broker_order_id not in broker_open_order_ids:
            try:
                fresh = broker.trading_client.get_order_by_id(broker_order_id)
                if isinstance(fresh, dict):
                    raise RuntimeError(f"Alpaca returned a raw dict instead of an Order object: {fresh!r}")
                db.record_order({
                    "client_order_id": row["client_order_id"], "broker_order_id": broker_order_id,
                    "symbol": row["symbol"], "asset_class": row["asset_class"], "side": row["side"],
                    "order_type": row["order_type"], "qty": row["qty"], "status": str(fresh.status),
                })
                discrepancies.append(
                    f"order {row['client_order_id']} was locally 'open' but broker reports "
                    f"{fresh.status} -- updated"
                )
            except Exception as e:
                discrepancies.append(
                    f"order {row['client_order_id']} could not be reconciled against broker: {e}"
                )

    still_open = db.open_orders()
    consistent = len(still_open) == 0
    db.set_risk_state("pending_reconciliation", not consistent)
    return ReconciliationResult(consistent=consistent, discrepancies=discrepancies)
