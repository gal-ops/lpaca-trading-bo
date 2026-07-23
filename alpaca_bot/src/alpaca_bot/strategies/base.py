"""Shared strategy interface (spec section 5). Every strategy module
exposes the same three functions operating on the same context/signal/
plan types, so the rest of the system (screening, probability models,
risk validator, execution) never needs to know which strategy produced a
given candidate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from alpaca_bot.data.bars import Bar


@dataclass
class StrategyContext:
    """Everything a strategy needs to evaluate one symbol on one cycle.
    `daily_bars`/`intraday_bars` are for the symbol itself; `benchmark_bars`
    is SPY for equities or BTC for crypto (relative-strength comparisons);
    `sector_bars` is optional (spec section 5, strategy 5's sector-ETF
    comparison) and may be None when no sector mapping is available."""

    symbol: str
    asset_class: str          # 'stock' or 'crypto'
    regime: str
    daily_bars: list[Bar]
    intraday_bars: list[Bar]
    benchmark_bars: list[Bar]
    sector_bars: list[Bar] | None = None
    relative_volume: float | None = None   # today's volume / N-day average
    spread_pct: float | None = None
    news_score: int | None = None          # -N..+N, from phase 6 strategy 4's classifier
    news_headline: str | None = None
    news_is_scheduled: bool = False
    minutes_since_session_open: float | None = None   # None outside a session
    cross_sectional_percentile: float | None = None   # this symbol's rank vs the eligible universe


@dataclass
class CandidateSignal:
    """Every field the spec requires a signal to carry (section 5)."""

    signal_id: str
    strategy: str
    symbol: str
    asset_class: str
    direction: str            # 'long' or 'short'
    regime: str
    entry: float
    stop: float
    target: float
    max_holding_seconds: float
    feature_snapshot: dict = field(default_factory=dict)
    raw_model_scores: dict = field(default_factory=dict)
    calibrated_probability: float | None = None    # filled in by phase 8
    expected_value_after_costs: float | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ValidationResult:
    valid: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class TradePlan:
    symbol: str
    asset_class: str
    direction: str
    entry: float
    stop: float
    target: float
    max_holding_seconds: float
    reward_risk: float
    strategy: str
    signal_id: str


def new_signal_id(strategy: str, symbol: str, direction: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{strategy}|{symbol}|{direction}|{ts}|{uuid.uuid4().hex[:8]}"


def reward_risk_ratio(entry: float, stop: float, target: float, direction: str) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0:
        return 0.0
    return reward / risk


MIN_REWARD_RISK = 1.5  # spec section 7's confidence-gate threshold; strategies
                        # self-filter to it too so weak setups never even
                        # reach the probability model.


def default_validate_candidate(candidate: CandidateSignal) -> ValidationResult:
    """Shared sanity checks every strategy's candidates must pass regardless
    of which strategy produced them: coherent direction/entry/stop/target,
    and the minimum reward/risk floor."""
    reasons = []
    if candidate.direction not in ("long", "short"):
        reasons.append(f"invalid direction {candidate.direction!r}")
    if candidate.direction == "long" and not (candidate.stop < candidate.entry < candidate.target):
        reasons.append("long candidate must have stop < entry < target")
    if candidate.direction == "short" and not (candidate.target < candidate.entry < candidate.stop):
        reasons.append("short candidate must have target < entry < stop")
    if candidate.asset_class == "crypto" and candidate.direction == "short":
        reasons.append("Alpaca crypto is spot-only -- shorting is never allowed")
    if not reasons:
        rr = reward_risk_ratio(candidate.entry, candidate.stop, candidate.target, candidate.direction)
        if rr < MIN_REWARD_RISK:
            reasons.append(f"reward/risk {rr:.2f} below minimum {MIN_REWARD_RISK}")
    return ValidationResult(valid=not reasons, reasons=reasons)


def default_build_trade_plan(candidate: CandidateSignal, validation: ValidationResult) -> TradePlan | None:
    if not validation.valid:
        return None
    rr = reward_risk_ratio(candidate.entry, candidate.stop, candidate.target, candidate.direction)
    return TradePlan(
        symbol=candidate.symbol, asset_class=candidate.asset_class, direction=candidate.direction,
        entry=candidate.entry, stop=candidate.stop, target=candidate.target,
        max_holding_seconds=candidate.max_holding_seconds, reward_risk=rr,
        strategy=candidate.strategy, signal_id=candidate.signal_id,
    )


class Strategy(Protocol):
    name: str

    def generate_candidate(self, context: StrategyContext) -> CandidateSignal | None: ...
    def validate_candidate(self, candidate: CandidateSignal, context: StrategyContext) -> ValidationResult: ...
    def build_trade_plan(self, candidate: CandidateSignal, context: StrategyContext) -> TradePlan | None: ...
