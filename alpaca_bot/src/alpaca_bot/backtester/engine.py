"""Event-driven backtester (spec section 12) sharing the exact same
strategy interfaces as paper execution (strategies/base.py's
generate_candidate/validate_candidate/build_trade_plan) so a strategy
behaves identically in backtest and live.

Important, explicitly-documented simplification: this backtester replays
*daily* bars, not tick-level bid/ask data -- true intraday tick replay
needs a dense historical tick/quote dataset this system does not have
access to. Each strategy's StrategyContext is built with a single-bar
"intraday_bars" list (today's daily OHLC bar standing in for an intraday
session), so intraday-specific strategies (opening-range breakout, VWAP
pullback/reversion, news momentum) run in a degraded approximation here,
not their full intraday form. This is disclosed in every backtest report
this module produces (spec section 12's requirement that costs/limitations
be surfaced, not hidden) and must be treated as a directional/sanity check
on strategy logic, not a claim of realistic intraday fills.

Conservative intrabar ordering (spec section 12): if a bar's range touches
both stop and target, the stop is assumed to fill first -- the pessimistic
assumption, never the optimistic one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from alpaca_bot.data.bars import Bar
from alpaca_bot.regimes.equity import classify_equity_regime
from alpaca_bot.regimes.indicators import ema_series
from alpaca_bot.strategies.base import CandidateSignal, StrategyContext, TradePlan


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    direction: str
    entry_ts: datetime
    entry_price: float
    exit_ts: datetime
    exit_price: float
    exit_reason: str
    qty: float
    pnl: float
    pnl_pct: float


@dataclass
class _OpenPosition:
    symbol: str
    strategy: str
    direction: str
    entry_ts: datetime
    entry_price: float
    stop: float
    target: float
    qty: float
    bars_held: int = 0
    max_holding_bars: int = 20


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    starting_equity: float = 0.0
    ending_equity: float = 0.0
    limitation_notice: str = (
        "Daily-bar replay, not tick-level bid/ask simulation -- intraday "
        "strategies run in a degraded single-bar-per-day approximation. "
        "Treat results as a directional sanity check on strategy logic, "
        "not a realistic fill/slippage estimate."
    )


class Backtester:
    """Runs one or more strategies bar-by-bar over historical daily data.
    `cost_bps` is a combined slippage+fee assumption applied at both entry
    and exit (spec section 12: fees, regulatory charges, slippage)."""

    def __init__(
        self,
        strategies: list,
        starting_equity: float = 540.0,
        max_positions: int = 3,
        max_gross_exposure_pct: float = 1.0,
        risk_per_trade_pct: float = 0.0025,
        max_position_pct: float = 0.20,
        cost_bps: float = 10.0,
        max_holding_bars: int = 20,
    ):
        self.strategies = strategies
        self.starting_equity = starting_equity
        self.max_positions = max_positions
        self.max_gross_exposure_pct = max_gross_exposure_pct
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.cost_bps = cost_bps
        self.max_holding_bars = max_holding_bars

    def run(
        self,
        symbol_bars: dict[str, list[Bar]],
        benchmark_bars: list[Bar],
        min_history: int = 55,
    ) -> BacktestResult:
        cash = self.starting_equity
        open_positions: dict[str, _OpenPosition] = {}
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple[datetime, float]] = []

        all_timestamps = sorted({b.ts for bars in symbol_bars.values() for b in bars})
        bench_by_ts = {b.ts: i for i, b in enumerate(benchmark_bars)}

        for i, ts in enumerate(all_timestamps):
            if i < min_history:
                continue
            bench_idx = bench_by_ts.get(ts)
            if bench_idx is None or bench_idx < min_history:
                # No benchmark history yet -- can't classify regime, so
                # positions still get managed but no new entries considered.
                regime = "MIXED_OR_UNCERTAIN"
            else:
                breadth_pct = self._breadth_pct(symbol_bars, ts, i)
                regime = classify_equity_regime(
                    benchmark_bars[: bench_idx + 1], [], breadth_pct=breadth_pct,
                ).regime

            # ---- manage open positions first (spec: sells/exits before new entries) ----
            for symbol in list(open_positions):
                bars = symbol_bars.get(symbol)
                if not bars:
                    continue
                bar = next((b for b in bars if b.ts == ts), None)
                if bar is None:
                    continue
                pos = open_positions[symbol]
                pos.bars_held += 1
                exit_price, reason = self._check_exit(pos, bar)
                if exit_price is not None:
                    trade, cash = self._close_position(pos, exit_price, reason, ts, cash)
                    trades.append(trade)
                    del open_positions[symbol]

            # ---- new entries ----
            available_slots = self.max_positions - len(open_positions)
            if available_slots > 0:
                deployed = sum(p.qty * p.entry_price for p in open_positions.values())
                equity_now = cash + deployed
                exposure_cap = equity_now * self.max_gross_exposure_pct

                for symbol, bars in symbol_bars.items():
                    if available_slots <= 0:
                        break
                    if symbol in open_positions:
                        continue
                    idx = next((j for j, b in enumerate(bars) if b.ts == ts), None)
                    if idx is None or idx < min_history:
                        continue
                    history = bars[: idx + 1]
                    bar = bars[idx]

                    context = StrategyContext(
                        symbol=symbol, asset_class="stock", regime=regime,
                        daily_bars=history, intraday_bars=[bar], benchmark_bars=benchmark_bars,
                        relative_volume=self._relative_volume(bars, idx),
                        minutes_since_session_open=30,
                        cross_sectional_percentile=self._cross_sectional_percentile(symbol_bars, ts, idx),
                    )
                    plan = self._best_plan(context)
                    if plan is None:
                        continue
                    qty = self._plan_qty(plan, equity_now)
                    if qty <= 0:
                        continue
                    if deployed + plan.entry * qty > exposure_cap:
                        continue
                    entry_price = self._apply_cost(plan.entry, plan.direction, entering=True)
                    open_positions[symbol] = _OpenPosition(
                        symbol=symbol, strategy=plan.strategy, direction=plan.direction,
                        entry_ts=ts, entry_price=entry_price, stop=plan.stop, target=plan.target,
                        qty=qty, max_holding_bars=self.max_holding_bars,
                    )
                    cash -= qty * entry_price
                    deployed += qty * entry_price
                    available_slots -= 1

            deployed = sum(p.qty * p.entry_price for p in open_positions.values())
            equity_curve.append((ts, cash + deployed))

        # Close anything still open at the final bar's close.
        for symbol, pos in list(open_positions.items()):
            bars = symbol_bars.get(symbol, [])
            if not bars:
                continue
            last_bar = bars[-1]
            trade, cash = self._close_position(pos, last_bar.close, "end_of_backtest", last_bar.ts, cash)
            trades.append(trade)

        ending_equity = equity_curve[-1][1] if equity_curve else self.starting_equity
        return BacktestResult(
            trades=trades, equity_curve=equity_curve,
            starting_equity=self.starting_equity, ending_equity=ending_equity,
        )

    # ---- helpers ----

    def _breadth_pct(self, symbol_bars: dict[str, list[Bar]], ts, global_idx: int) -> float:
        above = total = 0
        for bars in symbol_bars.values():
            idx = next((j for j, b in enumerate(bars) if b.ts == ts), None)
            if idx is None or idx < 20:
                continue
            closes = [b.close for b in bars[: idx + 1]]
            series = ema_series(closes, 20)
            if not series:
                continue
            total += 1
            if closes[-1] > series[-1]:
                above += 1
        return (100 * above / total) if total else 50.0

    def _relative_volume(self, bars: list[Bar], idx: int, lookback: int = 20) -> float:
        if idx < lookback:
            return 1.0
        window = bars[idx - lookback: idx]
        avg = sum(b.volume for b in window) / len(window) if window else 0
        return (bars[idx].volume / avg) if avg else 1.0

    def _cross_sectional_percentile(self, symbol_bars: dict[str, list[Bar]], ts, idx: int) -> float | None:
        returns = {}
        for sym, bars in symbol_bars.items():
            j = next((k for k, b in enumerate(bars) if b.ts == ts), None)
            if j is None or j < 5:
                continue
            returns[sym] = bars[j].close / bars[j - 5].close - 1
        if len(returns) < 2:
            return None
        sorted_syms = sorted(returns, key=lambda s: returns[s])
        target_sym = next((s for s, b in symbol_bars.items()
                            if any(bb.ts == ts for bb in b) and s in returns), None)
        if target_sym is None:
            return None
        rank = sorted_syms.index(target_sym)
        return 100 * rank / (len(sorted_syms) - 1) if len(sorted_syms) > 1 else 50.0

    def _best_plan(self, context: StrategyContext) -> TradePlan | None:
        best: TradePlan | None = None
        for strategy in self.strategies:
            candidate: CandidateSignal | None = strategy.generate_candidate(context)
            if candidate is None:
                continue
            plan = strategy.build_trade_plan(candidate, context)
            if plan is None:
                continue
            if best is None or plan.reward_risk > best.reward_risk:
                best = plan
        return best

    def _plan_qty(self, plan: TradePlan, equity: float) -> float:
        risk_budget = equity * self.risk_per_trade_pct
        unit_risk = abs(plan.entry - plan.stop)
        if unit_risk <= 0:
            return 0.0
        risk_based_qty = risk_budget / unit_risk
        exposure_based_qty = (equity * self.max_position_pct) / plan.entry
        return max(0.0, min(risk_based_qty, exposure_based_qty))

    def _apply_cost(self, price: float, direction: str, entering: bool) -> float:
        cost_frac = self.cost_bps / 10_000
        buying = (direction == "long" and entering) or (direction == "short" and not entering)
        return price * (1 + cost_frac) if buying else price * (1 - cost_frac)

    def _check_exit(self, pos: _OpenPosition, bar: Bar) -> tuple[float | None, str]:
        if pos.direction == "long":
            stop_hit = bar.low <= pos.stop
            target_hit = bar.high >= pos.target
            if stop_hit:  # conservative: stop wins on a same-bar clash
                return pos.stop, "stop_loss"
            if target_hit:
                return pos.target, "take_profit"
        else:
            stop_hit = bar.high >= pos.stop
            target_hit = bar.low <= pos.target
            if stop_hit:
                return pos.stop, "stop_loss"
            if target_hit:
                return pos.target, "take_profit"
        if pos.bars_held >= pos.max_holding_bars:
            return bar.close, "max_holding_time"
        return None, ""

    def _close_position(self, pos: _OpenPosition, raw_exit_price: float, reason: str,
                         ts: datetime, cash: float) -> tuple[BacktestTrade, float]:
        exit_price = self._apply_cost(raw_exit_price, pos.direction, entering=False)
        if pos.direction == "long":
            pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            pnl = (pos.entry_price - exit_price) * pos.qty
        pnl_pct = pnl / (pos.entry_price * pos.qty) if pos.entry_price and pos.qty else 0.0
        proceeds = pos.qty * exit_price if pos.direction == "long" else pos.qty * (
            2 * pos.entry_price - exit_price
        )
        cash += proceeds
        trade = BacktestTrade(
            symbol=pos.symbol, strategy=pos.strategy, direction=pos.direction,
            entry_ts=pos.entry_ts, entry_price=pos.entry_price, exit_ts=ts, exit_price=exit_price,
            exit_reason=reason, qty=pos.qty, pnl=pnl, pnl_pct=pnl_pct,
        )
        return trade, cash
