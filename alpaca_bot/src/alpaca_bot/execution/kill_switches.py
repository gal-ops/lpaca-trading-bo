"""Kill switches (spec section 11). Immediately blocks new orders, and
for the more severe conditions also flattens existing positions. The
safest failure mode, per the spec, is: cancel entries, preserve/restore
protective exits, reconcile positions, and stop -- never "try to keep
trading through it."

`should_flatten` distinguishes conditions where continuing to hold
existing positions is itself risky (a real loss-limit breach, positions
literally disagreeing between REST and the bot's own tracked state, an
unexplained equity jump, or an operator-requested STOP) from conditions
that are serious enough to halt new entries but don't necessarily mean
existing positions need to be closed immediately (a stream hiccup, a
rejection-rate spike, a schema mismatch). This split isn't stated
explicitly in the spec, so it's a judgment call, documented here rather
than left implicit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class KillSwitchInputs:
    daily_pnl_pct: float
    weekly_pnl_pct: float
    daily_stop_pct: float
    weekly_stop_pct: float
    consecutive_losses: int
    max_consecutive_losses: int
    market_data_stream_connected: bool
    trading_stream_connected: bool
    rest_positions: dict          # {symbol: qty} from the broker REST API
    tracked_positions: dict       # {symbol: qty} from the bot's own local/WS-derived state
    latest_quote_age_seconds: float
    max_quote_age_seconds: float
    clock_drift_seconds: float
    max_clock_drift_seconds: float
    recent_order_count: int
    recent_rejected_count: int
    max_rejection_rate: float
    observed_slippage: float
    modeled_slippage: float
    slippage_multiple_limit: float
    equity_now: float
    equity_expected: float
    max_unexpected_equity_change_pct: float
    db_write_healthy: bool
    duplicate_order_detected: bool
    model_schema_matches: bool
    calibration_monitoring_healthy: bool
    stop_file_present: bool
    position_qty_tolerance: float = 1e-6


@dataclass
class KillSwitchResult:
    triggered: bool
    should_flatten: bool
    reasons: list = field(default_factory=list)


def positions_disagree(rest: dict, tracked: dict, tolerance: float = 1e-6) -> bool:
    symbols = set(rest) | set(tracked)
    for symbol in symbols:
        if abs(rest.get(symbol, 0.0) - tracked.get(symbol, 0.0)) > tolerance:
            return True
    return False


def check_stop_file(path: str = "STOP") -> bool:
    return os.path.exists(path)


class KillSwitchMonitor:
    def evaluate(self, inputs: KillSwitchInputs) -> KillSwitchResult:
        reasons: list[str] = []
        flatten = False

        if inputs.daily_pnl_pct <= -inputs.daily_stop_pct:
            reasons.append(f"daily loss limit reached ({inputs.daily_pnl_pct:.2%})")
            flatten = True
        if inputs.weekly_pnl_pct <= -inputs.weekly_stop_pct:
            reasons.append(f"weekly loss limit reached ({inputs.weekly_pnl_pct:.2%})")
            flatten = True
        if inputs.consecutive_losses >= inputs.max_consecutive_losses:
            reasons.append(f"{inputs.consecutive_losses} consecutive losses reached")

        if not inputs.market_data_stream_connected:
            reasons.append("market-data stream disconnected")
        if not inputs.trading_stream_connected:
            reasons.append("trading stream disconnected")

        if positions_disagree(inputs.rest_positions, inputs.tracked_positions, inputs.position_qty_tolerance):
            reasons.append("REST and locally-tracked positions disagree")
            flatten = True

        if inputs.latest_quote_age_seconds > inputs.max_quote_age_seconds:
            reasons.append(
                f"quote timestamps stale ({inputs.latest_quote_age_seconds:.1f}s > "
                f"{inputs.max_quote_age_seconds:.1f}s)"
            )
        if inputs.clock_drift_seconds > inputs.max_clock_drift_seconds:
            reasons.append(
                f"excessive clock drift ({inputs.clock_drift_seconds:.1f}s > "
                f"{inputs.max_clock_drift_seconds:.1f}s)"
            )

        if inputs.recent_order_count > 0:
            rejection_rate = inputs.recent_rejected_count / inputs.recent_order_count
            if rejection_rate > inputs.max_rejection_rate:
                reasons.append(f"order rejection rate spike ({rejection_rate:.1%})")

        if inputs.modeled_slippage > 0 and inputs.observed_slippage > (
            inputs.slippage_multiple_limit * inputs.modeled_slippage
        ):
            reasons.append(
                f"observed slippage {inputs.observed_slippage:.4f} exceeds "
                f"{inputs.slippage_multiple_limit}x modeled {inputs.modeled_slippage:.4f}"
            )

        if inputs.equity_expected > 0:
            equity_change_pct = abs(inputs.equity_now - inputs.equity_expected) / inputs.equity_expected
            if equity_change_pct > inputs.max_unexpected_equity_change_pct:
                reasons.append(f"account equity changed unexpectedly ({equity_change_pct:.2%})")
                flatten = True

        if not inputs.db_write_healthy:
            reasons.append("database writes are failing")
        if inputs.duplicate_order_detected:
            reasons.append("duplicate-order protection failed")
        if not inputs.model_schema_matches:
            reasons.append("model file/feature schema mismatch")
        if not inputs.calibration_monitoring_healthy:
            reasons.append("calibration monitoring has failed")
        if inputs.stop_file_present:
            reasons.append("manual STOP file/dashboard kill switch is active")
            flatten = True

        return KillSwitchResult(triggered=bool(reasons), should_flatten=flatten and bool(reasons), reasons=reasons)
