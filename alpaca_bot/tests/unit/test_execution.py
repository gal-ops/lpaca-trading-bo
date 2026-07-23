"""Unit tests for order submission idempotency, reconciliation, and kill
switches (spec sections 10-11). All broker calls are mocked."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from alpaca_bot.broker.client import BrokerClient
from alpaca_bot.config import Settings
from alpaca_bot.execution.kill_switches import KillSwitchInputs, KillSwitchMonitor, positions_disagree
from alpaca_bot.execution.order_manager import OrderManager, make_client_order_id
from alpaca_bot.execution.reconciliation import reconcile
from alpaca_bot.persistence.db import Database
from alpaca_bot.strategies.base import CandidateSignal, TradePlan


def _settings() -> Settings:
    s = Settings.__new__(Settings)
    s.api_key = "test-key"
    s.secret_key = "test-secret"
    s.base_url = "https://paper-api.alpaca.markets"
    s.paper_trading_flag = "true"
    s.database_path = "unused.db"
    return s


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


@pytest.fixture
def broker():
    b = BrokerClient(_settings())
    b.trading_client = MagicMock()
    return b


def _candidate_and_plan(strategy="vwap_pullback_continuation", direction="long",
                        asset_class="stock", symbol="AAPL"):
    ts = datetime(2025, 1, 1, 9, 45, 0, tzinfo=timezone.utc)
    candidate = CandidateSignal(
        signal_id="sig-1", strategy=strategy, symbol=symbol, asset_class=asset_class,
        direction=direction, regime="BULL_TREND", entry=100.0, stop=98.0, target=104.0,
        max_holding_seconds=3600.0, created_at=ts,
    )
    plan = TradePlan(
        symbol=symbol, asset_class=asset_class, direction=direction, entry=100.0, stop=98.0,
        target=104.0, max_holding_seconds=3600.0, reward_risk=2.0, strategy=strategy,
        signal_id="sig-1",
    )
    return candidate, plan


# ---- client_order_id determinism ----

def test_client_order_id_is_deterministic_for_same_signal():
    candidate, _ = _candidate_and_plan()
    assert make_client_order_id(candidate) == make_client_order_id(candidate)


def test_client_order_id_differs_for_different_symbols():
    c1, _ = _candidate_and_plan(symbol="AAPL")
    c2, _ = _candidate_and_plan(symbol="MSFT")
    assert make_client_order_id(c1) != make_client_order_id(c2)


# ---- order manager: submission + idempotency ----

def _mock_order(order_id="order-1", status="filled", filled_qty="1", filled_avg_price="100.5"):
    order = MagicMock()
    order.id = order_id
    order.status = status
    order.filled_qty = filled_qty
    order.filled_avg_price = filled_avg_price
    return order


def test_submit_entry_records_order_and_fill(db, broker):
    candidate, plan = _candidate_and_plan()
    broker.trading_client.submit_order.return_value = _mock_order()
    broker.trading_client.get_order_by_id.return_value = _mock_order()

    manager = OrderManager(broker, db)
    result = manager.submit_entry(candidate, plan, qty=1.0)

    assert result.submitted is True
    assert result.filled is True
    assert result.filled_qty == 1.0
    order_row = db.query_one("SELECT * FROM orders WHERE client_order_id = ?", (result.client_order_id,))
    assert order_row is not None
    fill_row = db.query_one("SELECT * FROM fills WHERE client_order_id = ?", (result.client_order_id,))
    assert fill_row is not None
    position_row = db.query_one("SELECT * FROM positions WHERE symbol = ?", ("AAPL",))
    assert position_row is not None
    assert position_row["qty"] == 1.0


def test_submit_entry_is_idempotent_for_same_signal(db, broker):
    candidate, plan = _candidate_and_plan()
    broker.trading_client.submit_order.return_value = _mock_order()
    broker.trading_client.get_order_by_id.return_value = _mock_order()

    manager = OrderManager(broker, db)
    first = manager.submit_entry(candidate, plan, qty=1.0)
    second = manager.submit_entry(candidate, plan, qty=1.0)

    assert first.submitted is True
    assert second.submitted is False
    assert "duplicate" in second.reasons[0]
    assert broker.trading_client.submit_order.call_count == 1


def test_submit_entry_handles_rejection(db, broker):
    candidate, plan = _candidate_and_plan()
    broker.trading_client.submit_order.side_effect = Exception("insufficient buying power")

    manager = OrderManager(broker, db)
    result = manager.submit_entry(candidate, plan, qty=1.0)

    assert result.submitted is True
    assert result.filled is False
    assert result.status == "rejected"
    order_row = db.query_one("SELECT * FROM orders WHERE client_order_id = ?", (result.client_order_id,))
    assert order_row["status"] == "rejected"


def test_submit_entry_handles_partial_fill(db, broker):
    candidate, plan = _candidate_and_plan()
    broker.trading_client.submit_order.return_value = _mock_order(status="new")
    broker.trading_client.get_order_by_id.return_value = _mock_order(
        status="partially_filled", filled_qty="0.5", filled_avg_price="100.0",
    )

    manager = OrderManager(broker, db, fill_timeout_seconds=0.01)
    result = manager.submit_entry(candidate, plan, qty=1.0)

    assert result.filled is False  # not fully filled
    assert result.filled_qty == 0.5
    fill_row = db.query_one("SELECT * FROM fills WHERE client_order_id = ?", (result.client_order_id,))
    assert fill_row["qty"] == 0.5


def test_submit_entry_crypto_never_uses_bracket_order(db, broker):
    candidate, plan = _candidate_and_plan(asset_class="crypto", symbol="BTC/USD")
    broker.trading_client.submit_order.return_value = _mock_order()
    broker.trading_client.get_order_by_id.return_value = _mock_order()

    manager = OrderManager(broker, db)
    manager.submit_entry(candidate, plan, qty=0.01)

    submitted_request = broker.trading_client.submit_order.call_args[0][0]
    assert not hasattr(submitted_request, "order_class") or submitted_request.order_class is None or \
        "bracket" not in str(getattr(submitted_request, "order_class", "")).lower()


# ---- reconciliation ----

def _mock_position(symbol, qty="1", avg_entry_price="100"):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    p.avg_entry_price = avg_entry_price
    p.current_price = "101"
    p.market_value = "101"
    p.unrealized_pl = "1"
    p.unrealized_plpc = "0.01"
    return p


def test_reconcile_imports_broker_position_missing_locally(db, broker):
    broker.trading_client.get_all_positions.return_value = [_mock_position("AAPL")]
    broker.trading_client.get_orders.return_value = []

    result = reconcile(broker, db)
    assert any("importing it" in d for d in result.discrepancies)
    assert db.query_one("SELECT * FROM positions WHERE symbol = ?", ("AAPL",)) is not None


def test_reconcile_removes_local_position_no_longer_at_broker(db, broker):
    db.upsert_position({"symbol": "MSFT", "asset_class": "stock", "qty": 1, "avg_entry_price": 100})
    broker.trading_client.get_all_positions.return_value = []
    broker.trading_client.get_orders.return_value = []

    result = reconcile(broker, db)
    assert any("removing" in d for d in result.discrepancies)
    assert db.query_one("SELECT * FROM positions WHERE symbol = ?", ("MSFT",)) is None


def test_reconcile_updates_stale_open_order_status(db, broker):
    db.record_order({
        "client_order_id": "coid-1", "broker_order_id": "broker-1", "symbol": "AAPL",
        "asset_class": "stock", "side": "buy", "order_type": "market", "qty": 1, "status": "new",
    })
    broker.trading_client.get_all_positions.return_value = []
    broker.trading_client.get_orders.return_value = []  # no longer open at broker
    fresh_order = MagicMock()
    fresh_order.status = "filled"
    broker.trading_client.get_order_by_id.return_value = fresh_order

    result = reconcile(broker, db)
    assert result.consistent is True
    order_row = db.query_one("SELECT * FROM orders WHERE client_order_id = ?", ("coid-1",))
    assert order_row["status"] == "filled"


def test_reconcile_sets_pending_reconciliation_when_orders_remain_open(db, broker):
    db.record_order({
        "client_order_id": "coid-2", "broker_order_id": "broker-2", "symbol": "AAPL",
        "asset_class": "stock", "side": "buy", "order_type": "market", "qty": 1, "status": "new",
    })
    broker.trading_client.get_all_positions.return_value = []
    open_order = MagicMock()
    open_order.id = "broker-2"
    broker.trading_client.get_orders.return_value = [open_order]

    result = reconcile(broker, db)
    assert result.consistent is False
    assert db.get_risk_state("pending_reconciliation") is True


# ---- kill switches ----

def _healthy_inputs(**overrides) -> KillSwitchInputs:
    defaults = dict(
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, daily_stop_pct=0.0075, weekly_stop_pct=0.02,
        consecutive_losses=0, max_consecutive_losses=2,
        market_data_stream_connected=True, trading_stream_connected=True,
        rest_positions={"AAPL": 1.0}, tracked_positions={"AAPL": 1.0},
        latest_quote_age_seconds=1.0, max_quote_age_seconds=2.0,
        clock_drift_seconds=0.1, max_clock_drift_seconds=1.0,
        recent_order_count=10, recent_rejected_count=0, max_rejection_rate=0.5,
        observed_slippage=0.01, modeled_slippage=0.01, slippage_multiple_limit=2.0,
        equity_now=540.0, equity_expected=540.0, max_unexpected_equity_change_pct=0.05,
        db_write_healthy=True, duplicate_order_detected=False, model_schema_matches=True,
        calibration_monitoring_healthy=True, stop_file_present=False,
    )
    defaults.update(overrides)
    return KillSwitchInputs(**defaults)


def test_kill_switch_not_triggered_when_healthy():
    result = KillSwitchMonitor().evaluate(_healthy_inputs())
    assert result.triggered is False
    assert result.should_flatten is False


def test_kill_switch_daily_loss_triggers_and_flattens():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(daily_pnl_pct=-0.01))
    assert result.triggered is True
    assert result.should_flatten is True


def test_kill_switch_consecutive_losses_triggers_without_flatten():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(consecutive_losses=2))
    assert result.triggered is True
    assert result.should_flatten is False


def test_kill_switch_positions_disagree_flattens():
    result = KillSwitchMonitor().evaluate(
        _healthy_inputs(rest_positions={"AAPL": 2.0}, tracked_positions={"AAPL": 1.0})
    )
    assert result.triggered is True
    assert result.should_flatten is True


def test_kill_switch_stop_file_flattens():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(stop_file_present=True))
    assert result.triggered is True
    assert result.should_flatten is True


def test_kill_switch_stream_disconnect_blocks_without_flatten():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(market_data_stream_connected=False))
    assert result.triggered is True
    assert result.should_flatten is False


def test_kill_switch_rejection_rate_spike():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(recent_order_count=10, recent_rejected_count=8))
    assert result.triggered is True


def test_kill_switch_slippage_spike():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(observed_slippage=0.05, modeled_slippage=0.01))
    assert result.triggered is True


def test_kill_switch_unexpected_equity_change_flattens():
    result = KillSwitchMonitor().evaluate(_healthy_inputs(equity_now=400.0, equity_expected=540.0))
    assert result.triggered is True
    assert result.should_flatten is True


def test_positions_disagree_helper_respects_tolerance():
    assert positions_disagree({"AAPL": 1.0000001}, {"AAPL": 1.0}, tolerance=1e-4) is False
    assert positions_disagree({"AAPL": 1.1}, {"AAPL": 1.0}, tolerance=1e-4) is True
