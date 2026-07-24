"""Unit tests for the event-driven backtester and its metrics (spec
section 12). Uses deterministic synthetic price series -- no network
access or real historical data needed."""

from datetime import datetime, timedelta, timezone

from alpaca_bot.backtester.engine import Backtester, BacktestResult, BacktestTrade
from alpaca_bot.backtester.metrics import compute_metrics, meets_paper_release_gate
from alpaca_bot.data.bars import Bar
from alpaca_bot.strategies import ALL_STRATEGIES


def _series(closes: list[float], vol=5_000_000, wide=1.5) -> list[Bar]:
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return [
        Bar(ts=base + timedelta(days=i), open=c, high=c + wide, low=c - wide, close=c, volume=vol)
        for i, c in enumerate(closes)
    ]


def test_backtester_runs_end_to_end_on_trending_symbol():
    # A clean uptrend should let at least the relative-strength/pullback
    # strategies find something to do over 120 days.
    closes = [100 + 0.5 * i + (2 if i % 10 == 3 else 0) for i in range(120)]
    symbol_bars = {"TREND": _series(closes)}
    benchmark = _series([200 + 0.3 * i for i in range(120)])

    bt = Backtester(ALL_STRATEGIES, starting_equity=540.0, max_positions=3)
    result = bt.run(symbol_bars, benchmark, min_history=55)

    assert isinstance(result, BacktestResult)
    assert result.starting_equity == 540.0
    assert len(result.equity_curve) > 0
    # Every position must have been closed by the end.
    for trade in result.trades:
        assert isinstance(trade, BacktestTrade)
        assert trade.exit_ts >= trade.entry_ts

    # Trades must carry regime + feature_snapshot forward -- the model
    # trainer (phase 8) needs both to build TrainingExamples from
    # real backtest history.
    if result.trades:
        assert any(t.regime != "MIXED_OR_UNCERTAIN" or t.feature_snapshot for t in result.trades)


def test_backtester_never_exceeds_max_positions():
    closes_a = [100 + 0.5 * i for i in range(120)]
    closes_b = [50 + 0.3 * i for i in range(120)]
    closes_c = [30 + 0.2 * i for i in range(120)]
    symbol_bars = {
        "A": _series(closes_a), "B": _series(closes_b), "C": _series(closes_c),
    }
    benchmark = _series([200 + 0.3 * i for i in range(120)])

    bt = Backtester(ALL_STRATEGIES, starting_equity=540.0, max_positions=2)
    # Instrument via a subclassed run isn't necessary -- reconstruct open
    # position count at each timestamp indirectly by checking trades never
    # overlap more than max_positions at once.
    result = bt.run(symbol_bars, benchmark, min_history=55)

    events = []
    for t in result.trades:
        events.append((t.entry_ts, 1))
        events.append((t.exit_ts, -1))
    events.sort()
    concurrent = 0
    for _, delta in events:
        concurrent += delta
        assert concurrent <= 2


def test_backtester_never_uses_leverage():
    closes = [100 + 0.5 * i for i in range(120)]
    symbol_bars = {"TREND": _series(closes)}
    benchmark = _series([200 + 0.3 * i for i in range(120)])

    bt = Backtester(ALL_STRATEGIES, starting_equity=540.0, max_gross_exposure_pct=1.0)
    result = bt.run(symbol_bars, benchmark, min_history=55)
    # Equity should never go deeply negative from over-deployment (a sign
    # of implicit leverage) -- allow small cost-friction slack only.
    assert min(e for _, e in result.equity_curve) > 0


def test_compute_metrics_empty_result_is_neutral():
    result = BacktestResult(starting_equity=540.0, ending_equity=540.0)
    metrics = compute_metrics(result)
    assert metrics.n_trades == 0
    assert metrics.win_rate == 0.0


def test_compute_metrics_basic_win_loss_stats():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = [
        BacktestTrade("A", "s", "long", base, 100, base + timedelta(days=1), 110,
                       "take_profit", 1, pnl=10, pnl_pct=0.10),
        BacktestTrade("A", "s", "long", base, 100, base + timedelta(days=1), 95,
                       "stop_loss", 1, pnl=-5, pnl_pct=-0.05),
    ]
    result = BacktestResult(
        trades=trades, equity_curve=[(base, 540), (base + timedelta(days=1), 545)],
        starting_equity=540, ending_equity=545,
    )
    metrics = compute_metrics(result)
    assert metrics.n_trades == 2
    assert metrics.win_rate == 0.5
    assert metrics.avg_win == 10
    assert metrics.avg_loss == -5
    assert metrics.profit_factor == 2.0


def test_meets_paper_release_gate_fails_on_negative_expectancy():
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = [
        BacktestTrade("A", "s", "long", base, 100, base, 90, "stop_loss", 1, pnl=-10, pnl_pct=-0.1),
        BacktestTrade("A", "s", "long", base, 100, base, 92, "stop_loss", 1, pnl=-8, pnl_pct=-0.08),
    ]
    result = BacktestResult(trades=trades, equity_curve=[(base, 540), (base, 522)],
                             starting_equity=540, ending_equity=522)
    metrics = compute_metrics(result)
    ok, reasons = meets_paper_release_gate(metrics)
    assert ok is False
    assert any("expectancy" in r for r in reasons)


def test_meets_paper_release_gate_passes_healthy_result():
    # Profit spread across enough symbols that no single one exceeds the
    # 15% concentration cap, on top of a healthy profit factor/expectancy.
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = [
        BacktestTrade(sym, "s", "long", base, 100, base, 110, "take_profit", 1, pnl=10, pnl_pct=0.10)
        for sym in ["A", "B", "C", "D", "E", "F", "G", "H"]
    ] + [
        BacktestTrade("X", "s", "long", base, 100, base, 95, "stop_loss", 1, pnl=-5, pnl_pct=-0.05),
        BacktestTrade("Y", "s", "long", base, 100, base, 97, "stop_loss", 1, pnl=-3, pnl_pct=-0.03),
    ]
    equity_curve = [(base, 540), (base, 592)]
    result = BacktestResult(trades=trades, equity_curve=equity_curve, starting_equity=540, ending_equity=592)
    metrics = compute_metrics(result)
    ok, reasons = meets_paper_release_gate(metrics)
    assert ok is True
    assert reasons == []
