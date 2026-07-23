"""Unit tests for the independent pre-trade risk validator (spec section
8). Each of the 17 checks gets its own test, isolated from the others by
starting from a known-healthy baseline and breaking exactly one thing."""

from datetime import datetime, timedelta, timezone

import pytest

from alpaca_bot.risk.validator import FreshMarketState, PreTradeRiskValidator, RiskLimits
from alpaca_bot.strategies.base import CandidateSignal, TradePlan


def _healthy_fresh_state(**overrides) -> FreshMarketState:
    now = datetime.now(timezone.utc)
    defaults = dict(
        symbol_tradable=True, symbol_active=True, shortable=True, easy_to_borrow=True,
        latest_price=100.0, latest_quote_age_seconds=0.5, spread_pct=0.0005,
        regime_now="BULL_TREND", two_timeframes_confirm=True, session_permits_order_type=True,
        connections_healthy=True, has_existing_position=False, has_existing_open_order=False,
        account_equity=540.0, account_buying_power=540.0, deployed_usd=0.0,
        open_positions_count=0, correlated_cluster_exposure_usd=0.0,
        daily_pnl_pct=0.0, weekly_pnl_pct=0.0, consecutive_losses_today=0,
        signal_created_at=now - timedelta(seconds=5), now=now, stress_move_pct=0.02,
    )
    defaults.update(overrides)
    return FreshMarketState(**defaults)


def _candidate_and_plan(direction="long", asset_class="stock", regime="BULL_TREND") -> tuple:
    candidate = CandidateSignal(
        signal_id="sig-1", strategy="vwap_pullback_continuation", symbol="AAPL",
        asset_class=asset_class, direction=direction, regime=regime,
        entry=100.0, stop=98.0, target=104.0, max_holding_seconds=3600.0,
        created_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    plan = TradePlan(
        symbol="AAPL", asset_class=asset_class, direction=direction, entry=100.0, stop=98.0,
        target=104.0, max_holding_seconds=3600.0, reward_risk=2.0,
        strategy="vwap_pullback_continuation", signal_id="sig-1",
    )
    return candidate, plan


@pytest.fixture
def validator():
    return PreTradeRiskValidator(limits=RiskLimits())


def test_passes_on_fully_healthy_state(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state()
    # ~$50 notional on $540 equity (~9%) -- comfortably under every cap.
    result = validator.validate(candidate, plan, fresh, recent_signal_ids=set(), planned_qty=0.5)
    assert result.valid is True
    assert result.reasons == []


# 1. symbol active/tradable
def test_fails_when_symbol_no_longer_tradable(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(symbol_tradable=False)
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("tradable" in r for r in result.reasons)


# 2. crypto can't short
def test_fails_crypto_short_direction(validator):
    candidate, plan = _candidate_and_plan(direction="short", asset_class="crypto", regime="CRYPTO_BEAR_TREND")
    fresh = _healthy_fresh_state(regime_now="CRYPTO_BEAR_TREND")
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("spot-only" in r for r in result.reasons)


# 3. short eligibility
def test_fails_short_no_longer_borrowable(validator):
    candidate, plan = _candidate_and_plan(direction="short")
    fresh = _healthy_fresh_state(easy_to_borrow=False, regime_now="BULL_TREND")
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("shortable/easy-to-borrow" in r for r in result.reasons)


# 4. no conflicting position
def test_fails_with_existing_position(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(has_existing_position=True)
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("existing position" in r for r in result.reasons)


# 5. no duplicate signal / existing order
def test_fails_on_duplicate_signal_id(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state()
    result = validator.validate(candidate, plan, fresh, recent_signal_ids={"sig-1"}, planned_qty=5)
    assert result.valid is False
    assert any("duplicate order" in r for r in result.reasons)


def test_fails_with_existing_open_order(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(has_existing_open_order=True)
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("existing open order" in r for r in result.reasons)


# 6. signal expiry
def test_fails_on_expired_signal():
    validator = PreTradeRiskValidator(max_signal_age_seconds=10)
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(signal_created_at=datetime.now(timezone.utc) - timedelta(seconds=60))
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("old" in r for r in result.reasons)


# 7. regime change
def test_fails_when_regime_changed(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(regime_now="BEAR_TREND")
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("regime changed" in r for r in result.reasons)


# 8. timeframe confirmation
def test_fails_when_timeframes_no_longer_confirm(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(two_timeframes_confirm=False)
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("timeframes" in r for r in result.reasons)


# 9. spread expansion
def test_fails_on_wide_spread():
    validator = PreTradeRiskValidator(max_spread_pct=0.001)
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(spread_pct=0.01)
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("spread" in r for r in result.reasons)


# 10. slippage vs expectancy
def test_fails_when_slippage_would_destroy_expectancy():
    validator = PreTradeRiskValidator(max_spread_pct=1.0, min_slippage_survival_fraction=0.9)
    candidate, plan = _candidate_and_plan()  # risk = 2.0
    fresh = _healthy_fresh_state(spread_pct=0.5)  # huge relative spread
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("slippage" in r for r in result.reasons)


# 11. stop/target validity + plan drift
def test_fails_when_price_no_longer_between_stop_and_target(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(latest_price=97.0)  # below the stop already
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("bracket" in r for r in result.reasons)


def test_fails_when_plan_has_drifted_too_far():
    validator = PreTradeRiskValidator(plan_drift_tolerance_pct=0.01)
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(latest_price=103.0)  # 3% drift from planned entry of 100
    result = validator.validate(candidate, plan, fresh, set(), 5)
    assert result.valid is False
    assert any("drifted" in r for r in result.reasons)


# 12. position sizing / concentration limits
def test_fails_when_position_exceeds_max_equity_position_pct(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state()
    # 30% of $540 equity in one position, vs a 20% cap
    result = validator.validate(candidate, plan, fresh, set(), planned_qty=1.62)
    assert result.valid is False
    assert any("exceeds" in r and "of equity" in r for r in result.reasons)


def test_fails_at_max_simultaneous_positions(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(open_positions_count=3)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("max simultaneous positions" in r for r in result.reasons)


def test_fails_when_correlated_cluster_exposure_too_high(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(correlated_cluster_exposure_usd=200.0)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("correlated-cluster" in r for r in result.reasons)


def test_fails_when_exceeding_buying_power(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(account_buying_power=50.0)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("buying power" in r for r in result.reasons)


# 13. gross exposure cap (no leverage)
def test_fails_when_gross_exposure_would_exceed_100_pct(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(deployed_usd=500.0, account_equity=540.0)
    result = validator.validate(candidate, plan, fresh, set(), planned_qty=0.5)
    assert result.valid is False
    assert any("gross exposure" in r for r in result.reasons)


# 14. daily/weekly stops + consecutive losses
def test_fails_on_daily_stop_breach(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(daily_pnl_pct=-0.01)  # worse than -0.75% default stop
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("daily stop" in r for r in result.reasons)


def test_fails_on_weekly_stop_breach(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(weekly_pnl_pct=-0.03)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("weekly stop" in r for r in result.reasons)


def test_fails_on_consecutive_loss_limit(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(consecutive_losses_today=2)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("consecutive losses" in r for r in result.reasons)


# 15. session permits order type
def test_fails_when_session_does_not_permit_order_type(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(session_permits_order_type=False)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("session" in r for r in result.reasons)


# 16. data freshness / connection health
def test_fails_when_connections_unhealthy(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(connections_healthy=False)
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("connections" in r for r in result.reasons)


def test_fails_when_equity_quote_too_stale(validator):
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(latest_quote_age_seconds=5.0)  # > 2s equity threshold
    result = validator.validate(candidate, plan, fresh, set(), 1)
    assert result.valid is False
    assert any("quote is" in r for r in result.reasons)


def test_crypto_allows_longer_quote_age(validator):
    candidate, plan = _candidate_and_plan(asset_class="crypto")
    fresh = _healthy_fresh_state(latest_quote_age_seconds=4.0, regime_now="BULL_TREND")  # ok for crypto (<5s)
    result = validator.validate(candidate, plan, fresh, set(), planned_qty=0.5)
    assert result.valid is True


# 17. stress scenario
def test_fails_stress_scenario_when_stop_gap_risk_too_large():
    validator = PreTradeRiskValidator(limits=RiskLimits(risk_per_trade_pct=0.0025))
    candidate, plan = _candidate_and_plan()
    fresh = _healthy_fresh_state(stress_move_pct=0.5)  # extreme stress move
    result = validator.validate(candidate, plan, fresh, set(), planned_qty=5)
    assert result.valid is False
    assert any("stress scenario" in r for r in result.reasons)
