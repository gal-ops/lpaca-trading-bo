"""Unit tests for the SQLite persistence layer (spec section 13) and the
startup unreconciled-state check (spec section 1, rule 6)."""

import sqlite3
from datetime import datetime, timezone

import pytest

from alpaca_bot.persistence.db import Database
from alpaca_bot.universe.discovery import AssetRecord


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


def _asset_record(symbol="AAPL", **overrides):
    defaults = dict(
        asset_id=f"id-{symbol}", symbol=symbol, name=f"{symbol} Inc.",
        asset_class="us_equity", exchange="NASDAQ", status="active",
        tradable=True, fractionable=True, marginable=True, shortable=True,
        easy_to_borrow=True, maintenance_margin_requirement=0.25,
        min_order_increment=1, min_trade_increment=1, price_increment=0.01,
        last_checked=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return AssetRecord(**defaults)


def test_upsert_assets_inserts_and_updates(db):
    db.upsert_assets([_asset_record("AAPL")])
    row = db.query_one("SELECT * FROM assets WHERE symbol = ?", ("AAPL",))
    assert row["tradable"] == 1

    db.upsert_assets([_asset_record("AAPL", tradable=False)])
    row = db.query_one("SELECT * FROM assets WHERE symbol = ?", ("AAPL",))
    assert row["tradable"] == 0
    assert db.query_one("SELECT COUNT(*) AS n FROM assets")["n"] == 1


def test_record_and_query_signal(db):
    db.record_signal({
        "signal_id": "sig-1", "strategy": "vwap_pullback", "symbol": "AAPL",
        "asset_class": "us_equity", "direction": "long", "regime": "BULL_TREND",
        "entry": 190.0, "stop": 188.0, "target": 195.0,
        "calibrated_probability": 0.87, "expected_value_after_costs": 1.2,
        "accepted": True, "rejection_reasons": [],
    })
    row = db.query_one("SELECT * FROM signals WHERE signal_id = ?", ("sig-1",))
    assert row["accepted"] == 1
    assert row["calibrated_probability"] == 0.87


def test_order_lifecycle_and_open_orders(db):
    db.record_order({
        "client_order_id": "coid-1", "symbol": "AAPL", "asset_class": "us_equity",
        "side": "buy", "order_type": "limit", "qty": 1, "status": "new",
    })
    assert len(db.open_orders()) == 1

    db.record_order({
        "client_order_id": "coid-1", "symbol": "AAPL", "asset_class": "us_equity",
        "side": "buy", "order_type": "limit", "qty": 1, "status": "filled",
    })
    assert len(db.open_orders()) == 0

    db.record_fill("coid-1", "AAPL", "buy", 1, 190.5)
    fill = db.query_one("SELECT * FROM fills WHERE client_order_id = ?", ("coid-1",))
    assert fill["price"] == 190.5


def test_position_upsert_and_remove(db):
    db.upsert_position({
        "symbol": "AAPL", "asset_class": "us_equity", "qty": 1,
        "avg_entry_price": 190.0,
    })
    assert len(db.open_positions()) == 1
    db.remove_position("AAPL")
    assert len(db.open_positions()) == 0


def test_risk_state_roundtrip(db):
    assert db.get_risk_state("high_water_mark") is None
    db.set_risk_state("high_water_mark", 540.0)
    assert db.get_risk_state("high_water_mark") == 540.0


def test_has_unreconciled_state_false_when_clean(db):
    unreconciled, reason = db.has_unreconciled_state()
    assert unreconciled is False


def test_has_unreconciled_state_true_with_open_order(db):
    db.record_order({
        "client_order_id": "coid-2", "symbol": "AAPL", "asset_class": "us_equity",
        "side": "buy", "order_type": "market", "qty": 1, "status": "partially_filled",
    })
    unreconciled, reason = db.has_unreconciled_state()
    assert unreconciled is True
    assert "1 order" in reason


def test_has_unreconciled_state_true_with_pending_flag(db):
    db.set_risk_state("pending_reconciliation", True)
    unreconciled, reason = db.has_unreconciled_state()
    assert unreconciled is True


def test_calibration_bucket_roundtrip(db):
    assert db.get_calibration_bucket("vwap_pullback|long|equity|BULL_TREND") is None
    db.upsert_calibration_bucket("vwap_pullback|long|equity|BULL_TREND", n_examples=250)
    row = db.get_calibration_bucket("vwap_pullback|long|equity|BULL_TREND")
    assert row["n_examples"] == 250
    assert row["disabled"] == 0


def test_incremental_column_migration_adds_missing_column_to_existing_table(tmp_path):
    """Regression test: CREATE TABLE IF NOT EXISTS silently no-ops on a
    table that already exists, so a column added to schema.sql after a
    database was first created never gets applied on its own. Caught live
    when report generation failed with 'no such column: outcome_label'
    against a database created before that column was added."""
    db_path = str(tmp_path / "old_schema.db")

    # Simulate a database created before outcome_label existed.
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE signals (
            signal_id TEXT PRIMARY KEY, ts TEXT NOT NULL, strategy TEXT NOT NULL,
            symbol TEXT NOT NULL, asset_class TEXT NOT NULL, direction TEXT NOT NULL,
            regime TEXT, entry REAL, stop REAL, target REAL, max_holding_seconds REAL,
            feature_snapshot_json TEXT, raw_model_scores_json TEXT,
            calibrated_probability REAL, expected_value_after_costs REAL,
            accepted INTEGER NOT NULL, rejection_reasons_json TEXT, model_version TEXT
        )
    """)
    conn.commit()
    conn.close()

    database = Database(db_path)
    try:
        columns = {row[1] for row in database._conn.execute("PRAGMA table_info(signals)")}
        assert "outcome_label" in columns
        # And it's actually usable, not just present:
        database.record_signal({
            "signal_id": "sig-1", "strategy": "s", "symbol": "AAPL",
            "asset_class": "stock", "direction": "long", "accepted": True,
        })
        database.record_signal_outcome("sig-1", True)
        row = database.query_one("SELECT outcome_label FROM signals WHERE signal_id = ?", ("sig-1",))
        assert row["outcome_label"] == 1
    finally:
        database.close()
