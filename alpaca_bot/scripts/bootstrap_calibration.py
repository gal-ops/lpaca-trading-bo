#!/usr/bin/env python3
"""Bootstraps the confidence gate's calibration buckets from real
historical backtest data.

Why this exists: the confidence gate (spec section 7) requires 200+
real, outcome-labeled examples per strategy/direction/asset_class/regime
bucket before it will ever accept a signal. On a brand-new account there
is no live trade history to build that from -- and live trading can't
bootstrap it either, since the gate blocks every trade until a bucket is
already trained (a live-only approach is circular). The correct fix,
consistent with spec section 12, is to train from historical backtest
data instead: real past price action, run through the same strategy
logic, labeled by whether the resulting trade was net-profitable.

Two-stage, matching the spec's own staged screening pipeline (section
6): first a cheap 90-day liquidity screen over the full tradable equity
universe to rank and shortlist the most liquid symbols (expensive
per-symbol backtesting only happens on the shortlist, not all ~13k
names), then a full ~3-year daily-bar fetch + backtest on that
shortlist. A hand-picked ~48-symbol list was tried first and only
produced 45 trades in 750 days -- nowhere near enough to fill any
bucket; this scales the shortlist up until enough real signals fire.

Buckets that still don't reach 200 examples after this (e.g. a
strategy/regime combination that rarely fires) correctly remain
RESEARCH_ONLY_INSUFFICIENT_SAMPLE -- this script never lowers that
threshold to manufacture coverage.

Inherits the backtester's own disclosed limitation: daily-bar replay,
not tick-level fills, so intraday strategies are trained on a degraded
approximation of their real conditions. Re-run periodically as real
trade history accumulates and can gradually replace/reinforce this.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alpaca_bot.backtester.engine import Backtester  # noqa: E402
from alpaca_bot.broker.client import BrokerClient, PaperTradingSafetyError  # noqa: E402
from alpaca_bot.config import get_settings, load_strategy_config  # noqa: E402
from alpaca_bot.data.bars import get_daily_bars_batch  # noqa: E402
from alpaca_bot.models.training import TrainingExample, train_bucket_model  # noqa: E402
from alpaca_bot.persistence.db import Database  # noqa: E402
from alpaca_bot.strategies import ALL_STRATEGIES  # noqa: E402
from alpaca_bot.universe.discovery import discover_equity_universe, tradable_universe  # noqa: E402
from alpaca_bot.universe.screening import rank_candidates, screen_equity_universe  # noqa: E402

BENCHMARK_SYMBOL = "SPY"
SCREEN_LOOKBACK_DAYS = 90     # cheap liquidity prescreen window
BACKTEST_LOOKBACK_DAYS = 750  # ~3 years, for the actual backtest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortlist-size", type=int, default=500,
                         help="How many top-liquidity symbols to backtest.")
    args = parser.parse_args()

    settings = get_settings()
    cfg = load_strategy_config()
    min_examples = cfg["confidence_gate"]["min_out_of_sample_examples"]

    try:
        broker = BrokerClient(settings)
    except PaperTradingSafetyError as e:
        print(f"Refusing to start: {e}", file=sys.stderr)
        return 1

    print("Stage 1: discovering + screening the full tradable equity universe "
          f"(cheap {SCREEN_LOOKBACK_DAYS}-day liquidity prescreen)...")
    discovered = discover_equity_universe(broker)
    tradable = tradable_universe(discovered)
    print(f"  {len(discovered)} discovered, {len(tradable)} tradable.")

    screen_bars = get_daily_bars_batch(
        broker, [a.symbol for a in tradable], "stock", lookback_days=SCREEN_LOOKBACK_DAYS
    )
    eligibility = screen_equity_universe(tradable, screen_bars, cfg)
    ranked = rank_candidates(eligibility, top_n=args.shortlist_size)
    shortlist = [r.symbol for r in ranked]
    print(f"  {sum(1 for r in eligibility if r.eligible)} eligible, "
          f"shortlisting top {len(shortlist)} by liquidity for backtesting.")

    if not shortlist:
        print("Shortlist is empty -- cannot bootstrap. Aborting.", file=sys.stderr)
        return 1

    print(f"\nStage 2: fetching {BACKTEST_LOOKBACK_DAYS} days of daily bars for "
          f"{len(shortlist)} shortlisted symbols + benchmark ({BENCHMARK_SYMBOL})...")
    symbol_bars = get_daily_bars_batch(broker, shortlist, "stock", lookback_days=BACKTEST_LOOKBACK_DAYS)
    benchmark_bars = get_daily_bars_batch(
        broker, [BENCHMARK_SYMBOL], "stock", lookback_days=BACKTEST_LOOKBACK_DAYS
    ).get(BENCHMARK_SYMBOL, [])
    if not benchmark_bars:
        print(f"No benchmark data for {BENCHMARK_SYMBOL}; aborting.", file=sys.stderr)
        return 1
    print(f"  Fetched bars for {len(symbol_bars)}/{len(shortlist)} symbols.")

    bt = Backtester(ALL_STRATEGIES, starting_equity=540.0)
    result = bt.run(symbol_bars, benchmark_bars)
    print(f"\n{result.limitation_notice}\n")
    print(f"Backtest generated {len(result.trades)} trades across {len(symbol_bars)} symbols.")

    buckets: dict[str, list[TrainingExample]] = {}
    for trade in result.trades:
        key = f"{trade.strategy}|{trade.direction}|{trade.asset_class}|{trade.regime}"
        label = 1 if trade.pnl > 0 else 0
        buckets.setdefault(key, []).append(TrainingExample(
            bucket_key=key, features=trade.feature_snapshot, label=label, ts=trade.entry_ts,
        ))

    if not buckets:
        print("No trades were generated -- nothing to train. Every bucket remains "
              "RESEARCH_ONLY_INSUFFICIENT_SAMPLE.")
        return 0

    db = Database(settings.database_path)
    try:
        trained_count = 0
        for key, examples in sorted(buckets.items()):
            if len(examples) < min_examples:
                print(f"  {key}: {len(examples)} examples, below the {min_examples} minimum -- skipping.")
                continue
            model = train_bucket_model(examples, min_examples=min_examples)
            if model is None:
                print(f"  {key}: training failed (e.g. only one outcome class present) -- skipping.")
                continue
            db.upsert_calibration_bucket(
                key, n_examples=len(examples), model_version=model.model_version,
                calibration=asdict(model),
            )
            trained_count += 1
            print(f"  {key}: trained on {len(examples)} examples "
                  f"(test Brier={model.test_brier_score:.4f}, "
                  f"calibration error={model.test_calibration_error:.4f})")

        print(f"\n{trained_count}/{len(buckets)} buckets trained and stored in "
              f"{settings.database_path}. Buckets not listed above never fired enough "
              f"signals in this backtest to reach {min_examples} examples, and remain "
              f"RESEARCH_ONLY_INSUFFICIENT_SAMPLE until more history accumulates.")
        print("\nThis is a bootstrap from backtest data, not a claim of live profitability. "
              "See the limitation notice above.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
