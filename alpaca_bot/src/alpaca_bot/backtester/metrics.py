"""Performance metrics for a completed backtest (spec section 12):
win rate, avg win/loss, expectancy, profit factor, max drawdown,
Sharpe/Sortino. Also the minimum paper-release-gate checks from the same
section, evaluated against a BacktestResult."""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from alpaca_bot.backtester.engine import BacktestResult


@dataclass
class Metrics:
    n_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    expectancy: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    top_symbol_profit_share_pct: float


def compute_metrics(result: BacktestResult, periods_per_year: int = 252) -> Metrics:
    trades = result.trades
    n = len(trades)
    if n == 0:
        return Metrics(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss else float("inf") if gross_profit else 0.0

    equity_values = [e for _, e in result.equity_curve] or [result.starting_equity, result.ending_equity]
    max_drawdown_pct = _max_drawdown_pct(equity_values)

    daily_returns = _period_returns(equity_values)
    sharpe = _sharpe(daily_returns, periods_per_year)
    sortino = _sortino(daily_returns, periods_per_year)

    by_symbol: dict[str, float] = {}
    for t in trades:
        by_symbol[t.symbol] = by_symbol.get(t.symbol, 0.0) + t.pnl
    total_profit = sum(v for v in by_symbol.values() if v > 0)
    top_symbol_share = (max(by_symbol.values()) / total_profit * 100) if total_profit > 0 else 0.0

    return Metrics(
        n_trades=n, win_rate=win_rate, avg_win=avg_win, avg_loss=avg_loss,
        expectancy=expectancy, profit_factor=profit_factor, max_drawdown_pct=max_drawdown_pct,
        sharpe=sharpe, sortino=sortino, top_symbol_profit_share_pct=top_symbol_share,
    )


def _max_drawdown_pct(equity_values: list[float]) -> float:
    peak = equity_values[0]
    max_dd = 0.0
    for v in equity_values:
        peak = max(peak, v)
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)
    return max_dd * 100


def _period_returns(equity_values: list[float]) -> list[float]:
    returns = []
    for i in range(1, len(equity_values)):
        prev = equity_values[i - 1]
        if prev:
            returns.append((equity_values[i] - prev) / prev)
    return returns


def _sharpe(returns: list[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    stdev = statistics.pstdev(returns)
    if stdev == 0:
        return 0.0
    return (mean / stdev) * (periods_per_year ** 0.5)


def _sortino(returns: list[float], periods_per_year: int) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if mean > 0 else 0.0
    downside_dev = (sum(r ** 2 for r in downside) / len(returns)) ** 0.5
    if downside_dev == 0:
        return 0.0
    return (mean / downside_dev) * (periods_per_year ** 0.5)


def meets_paper_release_gate(metrics: Metrics, min_profit_factor: float = 1.25,
                               max_drawdown_pct: float = 8.0,
                               max_single_symbol_profit_share_pct: float = 15.0) -> tuple[bool, list[str]]:
    """Minimum paper-release criteria from spec section 12 that are
    computable from Metrics alone. Sample-size/calibration-stability
    criteria live with the probability-model layer (phase 8), not here."""
    reasons = []
    if metrics.expectancy <= 0:
        reasons.append(f"expectancy {metrics.expectancy:.4f} is not positive")
    if metrics.profit_factor < min_profit_factor:
        reasons.append(f"profit factor {metrics.profit_factor:.2f} below minimum {min_profit_factor}")
    if metrics.max_drawdown_pct > max_drawdown_pct:
        reasons.append(f"max drawdown {metrics.max_drawdown_pct:.2f}% exceeds {max_drawdown_pct}%")
    if metrics.top_symbol_profit_share_pct > max_single_symbol_profit_share_pct:
        reasons.append(
            f"top symbol contributes {metrics.top_symbol_profit_share_pct:.1f}% of profit, "
            f"exceeding {max_single_symbol_profit_share_pct}%"
        )
    return not reasons, reasons
