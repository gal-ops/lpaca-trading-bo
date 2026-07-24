#!/usr/bin/env python3
"""Runs the event-driven backtester over a symbol list using real
historical daily bars from Alpaca, and prints the resulting metrics plus
whether the run clears the spec's minimum paper-release gate.

Example:
    python scripts/backtest.py --symbols AAPL MSFT NVDA TSLA --days 400
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alpaca_bot.backtester.engine import Backtester  # noqa: E402
from alpaca_bot.backtester.metrics import compute_metrics, meets_paper_release_gate  # noqa: E402
from alpaca_bot.broker.client import BrokerClient, PaperTradingSafetyError  # noqa: E402
from alpaca_bot.config import get_settings  # noqa: E402
from alpaca_bot.data.bars import get_daily_bars_batch  # noqa: E402
from alpaca_bot.strategies import ALL_STRATEGIES  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--benchmark", default="SPY")
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--starting-equity", type=float, default=540.0)
    args = parser.parse_args()

    settings = get_settings()
    try:
        broker = BrokerClient(settings)
    except PaperTradingSafetyError as e:
        print(f"Refusing to start: {e}", file=sys.stderr)
        return 1

    symbol_bars = get_daily_bars_batch(broker, args.symbols, "stock", lookback_days=args.days)
    benchmark_bars = get_daily_bars_batch(broker, [args.benchmark], "stock", lookback_days=args.days).get(
        args.benchmark, []
    )
    if not benchmark_bars:
        print(f"No benchmark data for {args.benchmark}; aborting.", file=sys.stderr)
        return 1

    bt = Backtester(ALL_STRATEGIES, starting_equity=args.starting_equity)
    result = bt.run(symbol_bars, benchmark_bars)
    metrics = compute_metrics(result)
    ok, reasons = meets_paper_release_gate(metrics)

    print(f"\n{result.limitation_notice}\n")
    print(f"Trades: {metrics.n_trades}")
    print(f"Win rate: {metrics.win_rate:.1%}")
    print(f"Avg win / avg loss: ${metrics.avg_win:.2f} / ${metrics.avg_loss:.2f}")
    print(f"Expectancy: ${metrics.expectancy:.2f}")
    print(f"Profit factor: {metrics.profit_factor:.2f}")
    print(f"Max drawdown: {metrics.max_drawdown_pct:.2f}%")
    print(f"Sharpe (approx): {metrics.sharpe:.2f}")
    print(f"Sortino (approx): {metrics.sortino:.2f}")
    print(f"Top symbol profit share: {metrics.top_symbol_profit_share_pct:.1f}%")
    print(f"\nMeets minimum paper-release gate: {ok}")
    for r in reasons:
        print(f"  - {r}")

    print("\nThis is a backtest result, not a claim of future profitability. "
          "See the limitation notice above before drawing any conclusions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
