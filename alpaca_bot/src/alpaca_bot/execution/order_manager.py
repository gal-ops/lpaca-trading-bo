"""Deterministic, idempotent order handling (spec section 10).

A per-symbol lock prevents two overlapping cycles from submitting for the
same symbol at once; a deterministic client_order_id (derived from
strategy/symbol/signal timestamp/direction, not a random UUID) makes a
retried submission for the same signal a no-op rather than a duplicate
order; every order, event, and fill is persisted so submission is never
silently assumed to mean fill.

Equities use bracket orders (native OCO stop+target) where the broker
supports them. Alpaca's crypto order classes are simple only -- no native
bracket/OCO -- so crypto positions get a *synthetic* protective-exit
record (stop/target stored on the position row) that a separate monitor
loop (check_synthetic_protective_exits) watches and closes locally,
guarded by the same per-symbol lock so a stop and a target can never both
fire for the same position.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from alpaca_bot.broker.client import BrokerClient
from alpaca_bot.persistence.db import Database
from alpaca_bot.strategies.base import CandidateSignal, TradePlan

TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def _ensure_order_object(order):
    """alpaca-py's stubs type submit_order/get_order_by_id as returning
    Order | dict for a raw-response fallback; in practice this SDK always
    returns an Order object. Guards against silently treating a dict as
    one (which would fail loudly and unhelpfully on attribute access)."""
    if isinstance(order, dict):
        raise RuntimeError(f"Alpaca returned a raw dict instead of an Order object: {order!r}")
    return order


def make_client_order_id(candidate: CandidateSignal) -> str:
    """Deterministic, not random -- resubmitting for the same signal
    produces the same client_order_id, so Alpaca's own duplicate
    protection (and our own recent_signal_ids check) can catch a retry."""
    ts = candidate.created_at.strftime("%Y%m%dT%H%M%S")
    return f"{candidate.strategy[:12]}-{candidate.symbol.replace('/', '')}-{candidate.direction[:1]}-{ts}"[:128]


@dataclass
class SubmissionResult:
    submitted: bool
    filled: bool
    client_order_id: str
    broker_order_id: str | None
    filled_qty: float
    filled_avg_price: float | None
    status: str
    reasons: list


class SymbolLocks:
    """In-process per-symbol locks. A single bot process is the only
    writer to a given paper account here (no multi-process execution),
    so this is sufficient to prevent two overlapping cycles racing the
    same symbol; it is not a distributed lock."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def acquire(self, symbol: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.setdefault(symbol, threading.Lock())
        return lock


class OrderManager:
    def __init__(self, broker: BrokerClient, db: Database, fill_timeout_seconds: float = 8.0,
                 fill_poll_interval_seconds: float = 0.5):
        self.broker = broker
        self.db = db
        self.fill_timeout_seconds = fill_timeout_seconds
        self.fill_poll_interval_seconds = fill_poll_interval_seconds
        self.locks = SymbolLocks()

    def submit_entry(self, candidate: CandidateSignal, plan: TradePlan, qty: float,
                      use_bracket: bool = True, limit_price: float | None = None) -> SubmissionResult:
        symbol = plan.symbol
        lock = self.locks.acquire(symbol)
        if not lock.acquire(blocking=False):
            return SubmissionResult(False, False, "", None, 0.0, None, "locked",
                                     ["symbol is already being processed by another cycle"])
        try:
            client_order_id = make_client_order_id(candidate)

            existing = self.db.query_one(
                "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
            )
            if existing is not None:
                return SubmissionResult(
                    False, existing["status"] == "filled", client_order_id,
                    existing["broker_order_id"], 0.0, None, existing["status"],
                    ["duplicate client_order_id -- already submitted this cycle"],
                )

            # The orders table has a foreign key on signal_id -- record the
            # signal first (idempotent: INSERT OR IGNORE) so an order can
            # always be attributed back to the candidate that produced it.
            self.db.record_signal({
                "signal_id": candidate.signal_id, "strategy": candidate.strategy,
                "symbol": candidate.symbol, "asset_class": candidate.asset_class,
                "direction": candidate.direction, "regime": candidate.regime,
                "entry": candidate.entry, "stop": candidate.stop, "target": candidate.target,
                "max_holding_seconds": candidate.max_holding_seconds,
                "feature_snapshot": candidate.feature_snapshot,
                "raw_model_scores": candidate.raw_model_scores,
                "calibrated_probability": candidate.calibrated_probability,
                "expected_value_after_costs": candidate.expected_value_after_costs,
                "accepted": True, "rejection_reasons": candidate.rejection_reasons,
            })

            side = OrderSide.BUY if plan.direction == "long" else OrderSide.SELL
            is_crypto = plan.asset_class == "crypto"

            req: MarketOrderRequest | LimitOrderRequest
            if is_crypto:
                # No bracket orders for crypto -- protective exits are
                # synthetic (stop/target recorded on the position row and
                # watched separately, see check_synthetic_protective_exits).
                req = MarketOrderRequest(
                    symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.GTC,
                    client_order_id=client_order_id,
                )
            elif use_bracket:
                req = LimitOrderRequest(
                    symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                    limit_price=round(limit_price if limit_price is not None else plan.entry, 2),
                    order_class=OrderClass.BRACKET,
                    take_profit={"limit_price": round(plan.target, 2)},
                    stop_loss={"stop_price": round(plan.stop, 2)},
                    client_order_id=client_order_id,
                )
            else:
                req = MarketOrderRequest(
                    symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                    client_order_id=client_order_id,
                )

            self.db.record_order({
                "client_order_id": client_order_id, "symbol": symbol, "asset_class": plan.asset_class,
                "side": side.value, "order_type": "bracket" if (use_bracket and not is_crypto) else "market",
                "qty": qty, "limit_price": limit_price, "time_in_force": "day",
                "status": "new", "signal_id": candidate.signal_id,
            })
            self.db.record_order_event(client_order_id, "submitting")

            try:
                order = _ensure_order_object(self.broker.trading_client.submit_order(req))
            except Exception as e:
                self.db.record_order({
                    "client_order_id": client_order_id, "symbol": symbol, "asset_class": plan.asset_class,
                    "side": side.value, "order_type": "bracket" if use_bracket else "market", "qty": qty,
                    "status": "rejected", "signal_id": candidate.signal_id,
                })
                self.db.record_order_event(client_order_id, "rejected", {"error": str(e)})
                return SubmissionResult(True, False, client_order_id, None, 0.0, None, "rejected", [str(e)])

            self.db.record_order({
                "client_order_id": client_order_id, "broker_order_id": str(order.id), "symbol": symbol,
                "asset_class": plan.asset_class, "side": side.value,
                "order_type": "bracket" if use_bracket else "market", "qty": qty,
                "status": str(order.status), "signal_id": candidate.signal_id,
            })
            self.db.record_order_event(client_order_id, "accepted", {"broker_order_id": str(order.id)})

            filled = self._poll_until_terminal(order.id)
            filled_status = str(filled.status)
            filled_qty = float(filled.filled_qty or 0)
            filled_avg_price = float(filled.filled_avg_price) if filled.filled_avg_price else None

            self.db.record_order({
                "client_order_id": client_order_id, "broker_order_id": str(order.id), "symbol": symbol,
                "asset_class": plan.asset_class, "side": side.value,
                "order_type": "bracket" if use_bracket else "market", "qty": qty,
                "status": filled_status, "signal_id": candidate.signal_id,
            })
            self.db.record_order_event(client_order_id, "terminal", {"status": filled_status})

            if filled_qty > 0 and filled_avg_price is not None:
                self.db.record_fill(client_order_id, symbol, side.value, filled_qty, filled_avg_price)
                self.db.upsert_position({
                    "symbol": symbol, "asset_class": plan.asset_class, "qty": filled_qty,
                    "avg_entry_price": filled_avg_price, "strategy": plan.strategy,
                    "stop": plan.stop, "target": plan.target,
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                })

            return SubmissionResult(
                True, filled_status == "filled", client_order_id, str(order.id),
                filled_qty, filled_avg_price, filled_status, [],
            )
        finally:
            lock.release()

    def _poll_until_terminal(self, order_id):
        import time
        elapsed = 0.0
        order = _ensure_order_object(self.broker.trading_client.get_order_by_id(order_id))
        while str(order.status) not in TERMINAL_STATUSES and elapsed < self.fill_timeout_seconds:
            time.sleep(self.fill_poll_interval_seconds)
            elapsed += self.fill_poll_interval_seconds
            order = _ensure_order_object(self.broker.trading_client.get_order_by_id(order_id))
        return order

    def close_position(self, symbol: str, reason: str) -> SubmissionResult:
        lock = self.locks.acquire(symbol)
        if not lock.acquire(blocking=False):
            return SubmissionResult(False, False, "", None, 0.0, None, "locked",
                                     [f"symbol locked, cannot close for {reason}"])
        try:
            client_order_id = f"close-{symbol.replace('/', '')}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
            self.db.record_order({
                "client_order_id": client_order_id, "symbol": symbol, "asset_class": "unknown",
                "side": "sell", "order_type": "market", "status": "new",
            })
            try:
                order = _ensure_order_object(self.broker.trading_client.close_position(symbol))
            except Exception as e:
                self.db.record_order_event(client_order_id, "close_rejected", {"error": str(e), "reason": reason})
                return SubmissionResult(True, False, client_order_id, None, 0.0, None, "rejected", [str(e)])

            filled = self._poll_until_terminal(order.id)
            filled_qty = float(filled.filled_qty or 0)
            filled_avg_price = float(filled.filled_avg_price) if filled.filled_avg_price else None
            self.db.record_order_event(client_order_id, "closed", {"reason": reason, "status": str(filled.status)})
            if filled_qty > 0 and filled_avg_price is not None:
                self.db.record_fill(client_order_id, symbol, "sell", filled_qty, filled_avg_price)
                self.db.remove_position(symbol)
            return SubmissionResult(
                True, str(filled.status) == "filled", client_order_id, str(order.id),
                filled_qty, filled_avg_price, str(filled.status), [],
            )
        finally:
            lock.release()


def check_synthetic_protective_exits(positions: list, latest_prices: dict) -> list[tuple]:
    """Watches synthetic (locally-tracked) stop/target levels for crypto
    positions -- Alpaca crypto has no native bracket order, so this is the
    substitute. Returns [(symbol, reason)] for positions that should be
    closed this cycle. The caller (main loop) is responsible for actually
    closing them one at a time under the symbol lock, so a stop and a
    target can never race each other for the same position."""
    to_close = []
    for pos in positions:
        symbol = pos["symbol"]
        price = latest_prices.get(symbol)
        if price is None or pos["stop"] is None or pos["target"] is None:
            continue
        if pos["qty"] > 0:  # long
            if price <= pos["stop"]:
                to_close.append((symbol, "synthetic_stop_loss"))
            elif price >= pos["target"]:
                to_close.append((symbol, "synthetic_take_profit"))
    return to_close
