"""End-to-end replay of one full main._run_cycle -- discovery, screening,
regime classification, strategy evaluation, the confidence gate, the
risk validator, execution, reconciliation, kill switches, and report
generation -- all wired together against a fully mocked broker and a
real (temp-file) SQLite database. No network access or credentials.

This is deliberately distinct from the per-module unit tests: it proves
the phases actually integrate (naming, types, call signatures all line
up end to end), not just that each one works in isolation.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import alpaca_bot.main as main_module
from alpaca_bot.broker.client import BrokerClient
from alpaca_bot.config import Settings, load_strategy_config
from alpaca_bot.data.bars import Bar
from alpaca_bot.persistence.db import Database
from alpaca_bot.universe.discovery import AssetRecord


def _settings() -> Settings:
    s = Settings.__new__(Settings)
    s.api_key = "test-key"
    s.secret_key = "test-secret"
    s.base_url = "https://paper-api.alpaca.markets"
    s.paper_trading_flag = "true"
    s.database_path = "unused.db"
    return s


def _asset(symbol) -> AssetRecord:
    return AssetRecord(
        asset_id=f"id-{symbol}", symbol=symbol, name=symbol, asset_class="us_equity",
        exchange="NASDAQ", status="active", tradable=True, fractionable=True,
        marginable=True, shortable=True, easy_to_borrow=True,
        maintenance_margin_requirement=0.25, min_order_increment=1,
        min_trade_increment=1, price_increment=0.01, last_checked=datetime.now(timezone.utc),
    )


def _daily_bars(closes, wide=2.0):
    base = datetime.now(timezone.utc) - timedelta(days=len(closes))
    return [
        Bar(ts=base + timedelta(days=i), open=c, high=c + wide, low=c - wide, close=c, volume=5_000_000)
        for i, c in enumerate(closes)
    ]


def test_full_cycle_replay_runs_without_error_and_persists_state(tmp_path, monkeypatch):
    db_path = str(tmp_path / "replay.db")
    db = Database(db_path)

    broker = BrokerClient(_settings())
    broker.trading_client = MagicMock()
    broker.trading_client.get_all_positions.return_value = []
    broker.trading_client.get_orders.return_value = []

    account = MagicMock()
    account.equity = "540.00"
    account.cash = "440.00"
    account.buying_power = "440.00"
    account.last_equity = "540.00"
    # _run_cycle deliberately re-fetches a fresh account snapshot near the
    # end (broker.get_account()) rather than reusing the possibly-stale
    # one passed in -- must be mocked too, or MagicMock's default __float__
    # (1.0) silently leaks into the recorded pnl_snapshot.
    broker.trading_client.get_account.return_value = account

    aapl_closes = [190 + 0.5 * i for i in range(90)]
    spy_closes = [500 + 0.3 * i for i in range(90)]
    daily_bars = {"AAPL": _daily_bars(aapl_closes), "SPY": _daily_bars(spy_closes)}
    intraday_bars = {"AAPL": _daily_bars(aapl_closes[-8:])}

    monkeypatch.setattr(main_module, "discover_equity_universe", lambda broker: [_asset("AAPL")])
    monkeypatch.setattr(main_module, "discover_crypto_universe", lambda broker: [])
    monkeypatch.setattr(main_module, "get_daily_bars_batch",
                         lambda broker, symbols, asset_class, lookback_days=90: daily_bars)
    monkeypatch.setattr(main_module, "get_intraday_bars_batch",
                         lambda broker, symbols, asset_class, minutes, lookback_bars: intraday_bars)

    cfg = load_strategy_config()

    main_module._run_cycle(broker, db, cfg, account)

    # SPY is benchmark bar data only, never part of the discovered
    # universe, so it's correctly absent from the assets table.
    assets = db.query("SELECT symbol FROM assets")
    assert {a["symbol"] for a in assets} == {"AAPL"}

    regime_row = db.latest_regime("equity")
    assert regime_row is not None
    assert regime_row["regime"] in (
        "BULL_TREND", "BEAR_TREND", "RANGE", "HIGH_VOLATILITY_OR_SHOCK", "MIXED_OR_UNCERTAIN",
    )

    pnl_row = db.query_one("SELECT * FROM pnl_snapshots ORDER BY ts DESC LIMIT 1")
    assert pnl_row is not None
    assert pnl_row["equity"] == 540.0

    # Any signals generated this cycle must have been persisted, whichever
    # way the confidence gate/risk validator decided -- with zero
    # calibration history, none should have been accepted (spec section 7).
    signals = db.query("SELECT * FROM signals")
    for s in signals:
        if s["accepted"]:
            assert False, "no signal should be accepted with zero calibration history"

    db.close()
