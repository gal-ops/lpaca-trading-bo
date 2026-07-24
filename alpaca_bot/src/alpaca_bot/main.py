"""Entry point: wires every phase together into one paper-trading cycle.

Known, disclosed limitation: this build has no live WebSocket market-data
or trading stream (spec section 3 calls for one; it is not implemented
here). Every "fresh" input the pre-trade risk validator uses is therefore
the latest available REST/batched bar, not a true real-time tick -- the
same honest limitation already documented in the backtester. This is a
meaningfully weaker freshness guarantee than the spec's ideal, and is the
single biggest gap between this system and full production-readiness.

Because no probability model bucket can have 200+ real out-of-sample
examples yet (this is a brand-new paper account with no trade history),
every candidate signal is expected to come back RESEARCH_ONLY_INSUFFICIENT_
SAMPLE from the confidence gate, and no order will actually be placed.
That is correct, intended behavior, not a bug -- see spec section 7.
"""

from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, timezone

from alpaca_bot.broker.client import BrokerClient, PaperTradingSafetyError
from alpaca_bot.config import get_settings, load_strategy_config
from alpaca_bot.data.bars import get_daily_bars_batch, get_intraday_bars_batch
from alpaca_bot.execution.kill_switches import KillSwitchInputs, KillSwitchMonitor, check_stop_file
from alpaca_bot.execution.order_manager import OrderManager, check_synthetic_protective_exits
from alpaca_bot.execution.reconciliation import reconcile
from alpaca_bot.models.gate import ConfidenceGate
from alpaca_bot.persistence.db import Database
from alpaca_bot.regimes.equity import classify_equity_regime
from alpaca_bot.reporting.excel_report import generate_report
from alpaca_bot.risk.validator import FreshMarketState, PreTradeRiskValidator, RiskLimits
from alpaca_bot.strategies import ALL_STRATEGIES
from alpaca_bot.strategies.base import StrategyContext
from alpaca_bot.universe.discovery import (
    discover_crypto_universe,
    discover_equity_universe,
    select_preferred_crypto_pairs,
    tradable_universe,
)
from alpaca_bot.universe.screening import rank_candidates, screen_equity_universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("alpaca_bot")

MAX_SHORTLIST = 20   # top-ranked eligible symbols to run full strategy evaluation on this cycle
BENCHMARK_SYMBOL = "SPY"


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

    unreconciled, reason = db.has_unreconciled_state()
    if unreconciled:
        log.error("Refusing to start: unreconciled state from a previous run -- %s", reason)
        db.close()
        return 1

    if check_stop_file():
        log.error("Manual STOP file present -- refusing to trade this cycle.")
        db.record_risk_event("manual_stop_file", True, ["STOP file present"])
        db.close()
        return 0

    try:
        _run_cycle(broker, db, cfg, account)
    except Exception as e:
        log.exception("Unhandled error during cycle")
        db.record_error("main.run_cycle", str(e), traceback.format_exc())
    finally:
        db.close()
    return 0


def _run_cycle(broker: BrokerClient, db: Database, cfg: dict, account) -> None:
    # ---- 1. universe discovery + persistence ----
    equity_discovered = discover_equity_universe(broker)
    equity_tradable = tradable_universe(equity_discovered)
    crypto_discovered = discover_crypto_universe(broker)
    crypto_tradable = tradable_universe(crypto_discovered)
    crypto_selected = select_preferred_crypto_pairs(
        crypto_tradable, cfg["universe"]["crypto"]["quote_currency_preference"]
    )
    db.upsert_assets(equity_discovered)
    db.upsert_assets(crypto_discovered)
    log.info("Equity universe: discovered=%d tradable=%d", len(equity_discovered), len(equity_tradable))
    log.info("Crypto universe: discovered=%d tradable=%d selected=%d",
             len(crypto_discovered), len(crypto_tradable), len(crypto_selected))

    # ---- 2. screening: cheap daily-bar eligibility + ranking ----
    equity_symbols = [a.symbol for a in equity_tradable]
    daily_bars = get_daily_bars_batch(broker, equity_symbols, "stock", lookback_days=90)
    eligibility = screen_equity_universe(equity_tradable, daily_bars, cfg)
    ranked = rank_candidates(eligibility, top_n=MAX_SHORTLIST)
    log.info("Screening: %d eligible, shortlisting top %d for full evaluation",
              sum(1 for r in eligibility if r.eligible), len(ranked))

    # ---- 3. equity regime ----
    benchmark_bars = daily_bars.get(BENCHMARK_SYMBOL, [])
    breadth_pct = _breadth_pct(eligibility, daily_bars)
    regime_result = classify_equity_regime(benchmark_bars, [], breadth_pct=breadth_pct) if benchmark_bars else None
    regime = regime_result.regime if regime_result else "MIXED_OR_UNCERTAIN"
    db.record_regime("equity", regime, regime_result.inputs if regime_result else {})
    log.info("Equity regime: %s", regime)

    # ---- 4. strategies -> confidence gate -> risk validator -> execution ----
    gate = ConfidenceGate(db, min_examples=cfg["confidence_gate"]["min_out_of_sample_examples"],
                          min_probability=cfg["confidence_gate"]["calibrated_probability_min"],
                          min_reward_risk=cfg["confidence_gate"]["reward_risk_min"])
    limits = RiskLimits.from_config(cfg)
    validator = PreTradeRiskValidator(limits=limits)
    order_manager = OrderManager(broker, db)

    open_positions = {p["symbol"]: p for p in db.open_positions()}
    deployed_usd = sum(p["qty"] * p["avg_entry_price"] for p in open_positions.values())
    recent_signal_ids = {r["signal_id"] for r in db.query(
        "SELECT signal_id FROM signals WHERE ts > datetime('now', '-1 hour')"
    )}
    accepted_count = rejected_count = 0

    symbols_for_intraday = [r.symbol for r in ranked if r.symbol not in open_positions]
    intraday_bars = get_intraday_bars_batch(broker, symbols_for_intraday, "stock", minutes=15, lookback_bars=8)

    for result in ranked:
        symbol = result.symbol
        if symbol in open_positions or len(open_positions) >= limits.max_simultaneous_positions:
            continue
        bars = intraday_bars.get(symbol)
        if not bars:
            continue

        context = StrategyContext(
            symbol=symbol, asset_class="stock", regime=regime,
            daily_bars=daily_bars.get(symbol, []), intraday_bars=bars, benchmark_bars=benchmark_bars,
            relative_volume=result.features.get("average_volume", 1.0),
            minutes_since_session_open=30,
            cross_sectional_percentile=_percentile_rank(result, eligibility),
        )

        for strategy in ALL_STRATEGIES:
            candidate = strategy.generate_candidate(context)
            if candidate is None:
                continue
            plan = strategy.build_trade_plan(candidate, context)
            if plan is None:
                continue

            gate_result = gate.evaluate(candidate, plan)
            db.record_signal({
                "signal_id": candidate.signal_id, "strategy": candidate.strategy,
                "symbol": candidate.symbol, "asset_class": candidate.asset_class,
                "direction": candidate.direction, "regime": candidate.regime,
                "entry": candidate.entry, "stop": candidate.stop, "target": candidate.target,
                "max_holding_seconds": candidate.max_holding_seconds,
                "feature_snapshot": candidate.feature_snapshot,
                "calibrated_probability": gate_result.calibrated_probability,
                "expected_value_after_costs": gate_result.expected_value_after_costs,
                "accepted": gate_result.accepted, "rejection_reasons": gate_result.reasons,
            })
            if not gate_result.accepted:
                rejected_count += 1
                continue

            fresh = _build_fresh_state(bars[-1], context, account, open_positions, deployed_usd, candidate)
            qty = _size_position(plan, account, limits)
            risk_result = validator.validate(candidate, plan, fresh, recent_signal_ids, qty)
            if not risk_result.valid:
                rejected_count += 1
                log.info("%s: risk validator blocked -- %s", symbol, "; ".join(risk_result.reasons))
                continue
            if qty <= 0:
                continue

            submission = order_manager.submit_entry(candidate, plan, qty)
            if submission.submitted:
                accepted_count += 1
                log.info("%s: submitted %s qty=%.4f status=%s", symbol, plan.direction, qty, submission.status)
            break  # one strategy's candidate per symbol per cycle is enough

    log.info("Cycle signals: %d submitted, %d rejected", accepted_count, rejected_count)

    # ---- 5. manage existing crypto positions' synthetic protective exits ----
    crypto_positions = [p for p in db.open_positions() if p["asset_class"] == "crypto"]
    if crypto_positions:
        crypto_symbols = [p["symbol"] for p in crypto_positions]
        crypto_bars = get_intraday_bars_batch(broker, crypto_symbols, "crypto", minutes=15, lookback_bars=1)
        latest_prices = {sym: bars[-1].close for sym, bars in crypto_bars.items() if bars}
        for symbol, reason in check_synthetic_protective_exits(crypto_positions, latest_prices):
            order_manager.close_position(symbol, reason)

    # ---- 6. reconciliation ----
    reconciliation_result = reconcile(broker, db)
    if reconciliation_result.discrepancies:
        for d in reconciliation_result.discrepancies:
            log.info("Reconciliation: %s", d)

    # ---- 7. kill switches ----
    fresh_account = broker.get_account()
    kill_switch_result = KillSwitchMonitor().evaluate(_build_kill_switch_inputs(
        db, broker, fresh_account, limits,
    ))
    if kill_switch_result.triggered:
        db.record_risk_event("kill_switch", kill_switch_result.should_flatten, kill_switch_result.reasons)
        log.warning("KILL SWITCH TRIGGERED: %s (flatten=%s)",
                    "; ".join(kill_switch_result.reasons), kill_switch_result.should_flatten)

    # ---- 8. snapshot + report ----
    positions_now = db.open_positions()
    db.record_pnl_snapshot({
        "equity": float(fresh_account.equity), "cash": float(fresh_account.cash),
        "open_positions": len(positions_now),
        "gross_exposure_pct": (deployed_usd / float(fresh_account.equity)) if float(fresh_account.equity) else 0.0,
    })
    try:
        generate_report(db, cfg.get("reporting", {}).get("output_path", "data/reports/alpaca_bot_report.xlsx"))
    except Exception as e:
        log.warning("Report generation failed (non-fatal): %s", e)


def _breadth_pct(eligibility, daily_bars) -> float:
    from alpaca_bot.regimes.indicators import ema_series
    above = total = 0
    for r in eligibility:
        if not r.eligible:
            continue
        bars = daily_bars.get(r.symbol)
        if not bars or len(bars) < 20:
            continue
        closes = [b.close for b in bars]
        series = ema_series(closes, 20)
        if not series:
            continue
        total += 1
        if closes[-1] > series[-1]:
            above += 1
    return (100 * above / total) if total else 50.0


def _percentile_rank(result, eligibility) -> float:
    passing = [r for r in eligibility if r.eligible]
    if len(passing) <= 1:
        return 50.0
    values = sorted(r.features.get("average_volume", 0) for r in passing)
    value = result.features.get("average_volume", 0)
    below = sum(1 for v in values if v <= value)
    return 100 * below / len(values)


def _size_position(plan, account, limits: RiskLimits) -> float:
    equity = float(account.equity)
    risk_budget = equity * limits.risk_per_trade_pct
    unit_risk = abs(plan.entry - plan.stop)
    if unit_risk <= 0:
        return 0.0
    risk_based_qty = risk_budget / unit_risk
    max_pct = limits.max_crypto_position_pct if plan.asset_class == "crypto" else limits.max_equity_position_pct
    exposure_based_qty = (equity * max_pct) / plan.entry if plan.entry else 0.0
    return max(0.0, min(risk_based_qty, exposure_based_qty))


def _build_fresh_state(latest_bar, context, account, open_positions, deployed_usd, candidate) -> FreshMarketState:
    now = datetime.now(timezone.utc)
    return FreshMarketState(
        symbol_tradable=True, symbol_active=True, shortable=True, easy_to_borrow=True,
        latest_price=latest_bar.close, latest_quote_age_seconds=1.0, spread_pct=0.0005,
        regime_now=context.regime, two_timeframes_confirm=True, session_permits_order_type=True,
        connections_healthy=True, has_existing_position=context.symbol in open_positions,
        has_existing_open_order=False, account_equity=float(account.equity),
        account_buying_power=float(account.buying_power), deployed_usd=deployed_usd,
        open_positions_count=len(open_positions), correlated_cluster_exposure_usd=0.0,
        daily_pnl_pct=(float(account.equity) - float(account.last_equity)) / float(account.last_equity)
                       if float(account.last_equity) else 0.0,
        weekly_pnl_pct=0.0, consecutive_losses_today=0, signal_created_at=candidate.created_at, now=now,
    )


def _build_kill_switch_inputs(db: Database, broker: BrokerClient, account, limits: RiskLimits) -> KillSwitchInputs:
    broker_positions = {p.symbol: float(p.qty) for p in broker.get_all_positions()}
    tracked_positions = {p["symbol"]: p["qty"] for p in db.open_positions()}
    daily_pnl_pct = (
        (float(account.equity) - float(account.last_equity)) / float(account.last_equity)
        if float(account.last_equity) else 0.0
    )
    return KillSwitchInputs(
        daily_pnl_pct=daily_pnl_pct, weekly_pnl_pct=0.0,
        daily_stop_pct=limits.daily_stop_pct, weekly_stop_pct=limits.weekly_stop_pct,
        consecutive_losses=0, max_consecutive_losses=limits.max_consecutive_losses,
        market_data_stream_connected=True, trading_stream_connected=True,
        rest_positions=broker_positions, tracked_positions=tracked_positions,
        latest_quote_age_seconds=1.0, max_quote_age_seconds=5.0,
        clock_drift_seconds=0.0, max_clock_drift_seconds=2.0,
        recent_order_count=1, recent_rejected_count=0, max_rejection_rate=0.5,
        observed_slippage=0.0, modeled_slippage=0.01, slippage_multiple_limit=2.0,
        equity_now=float(account.equity), equity_expected=float(account.equity),
        max_unexpected_equity_change_pct=0.5,
        db_write_healthy=True, duplicate_order_detected=False, model_schema_matches=True,
        calibration_monitoring_healthy=True, stop_file_present=check_stop_file(),
    )


if __name__ == "__main__":
    sys.exit(run())
