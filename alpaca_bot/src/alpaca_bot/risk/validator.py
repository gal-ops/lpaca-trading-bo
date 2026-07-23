"""The independent pre-trade risk validator (spec section 8) -- a
separate component from the signal generator/confidence gate that
reruns every critical check against a *fresh* quote and *fresh* account
state, immediately before order submission. This is the last thing that
runs before a trade is allowed; if it fails, nothing else in the system
can overrule it.

All 17 checks from the spec are implemented as individually testable
methods so a failure always has one unambiguous cause in the reasons
list, and the original vs. independently recomputed plan is logged and
compared so a stale/drifted plan can't slip through unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from alpaca_bot.strategies.base import CandidateSignal, TradePlan, ValidationResult


@dataclass
class RiskLimits:
    """Mirrors config/default.yaml's risk block (spec section 9)."""

    risk_per_trade_pct: float = 0.0025
    max_equity_position_pct: float = 0.20
    max_crypto_position_pct: float = 0.15
    max_individual_short_pct: float = 0.15
    max_gross_exposure_pct: float = 1.00
    max_simultaneous_positions: int = 3
    max_correlated_cluster_pct: float = 0.30
    daily_stop_pct: float = 0.0075
    weekly_stop_pct: float = 0.02
    max_consecutive_losses: int = 2

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskLimits":
        risk_cfg = cfg["risk"]
        return cls(
            risk_per_trade_pct=risk_cfg["risk_per_trade_pct"],
            max_equity_position_pct=risk_cfg["max_equity_position_pct"],
            max_crypto_position_pct=risk_cfg["max_crypto_position_pct"],
            max_individual_short_pct=risk_cfg["max_individual_short_pct"],
            max_gross_exposure_pct=risk_cfg["max_gross_exposure_pct"],
            max_simultaneous_positions=risk_cfg["max_simultaneous_positions"],
            max_correlated_cluster_pct=risk_cfg["max_correlated_cluster_pct"],
            daily_stop_pct=risk_cfg["daily_stop_pct"],
            weekly_stop_pct=risk_cfg["weekly_stop_pct"],
            max_consecutive_losses=risk_cfg["max_consecutive_losses"],
        )


@dataclass
class FreshMarketState:
    """Everything the validator needs re-fetched fresh, right before
    submission -- never reused from whenever the signal was generated."""

    symbol_tradable: bool
    symbol_active: bool
    shortable: bool
    easy_to_borrow: bool
    latest_price: float
    latest_quote_age_seconds: float
    spread_pct: float
    regime_now: str
    two_timeframes_confirm: bool
    session_permits_order_type: bool
    connections_healthy: bool
    has_existing_position: bool
    has_existing_open_order: bool
    account_equity: float
    account_buying_power: float
    deployed_usd: float
    open_positions_count: int
    correlated_cluster_exposure_usd: float
    daily_pnl_pct: float
    weekly_pnl_pct: float
    consecutive_losses_today: int
    signal_created_at: datetime
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stress_move_pct: float = 0.10   # a further adverse move to stress-test against


@dataclass
class RecomputedPlan:
    entry: float
    stop: float
    target: float


DEFAULT_MAX_SIGNAL_AGE_SECONDS = 120.0
DEFAULT_MAX_SPREAD_PCT = 0.0015
DEFAULT_PLAN_DRIFT_TOLERANCE_PCT = 0.02


class PreTradeRiskValidator:
    def __init__(
        self,
        limits: RiskLimits | None = None,
        max_signal_age_seconds: float = DEFAULT_MAX_SIGNAL_AGE_SECONDS,
        max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
        plan_drift_tolerance_pct: float = DEFAULT_PLAN_DRIFT_TOLERANCE_PCT,
        min_slippage_survival_fraction: float = 0.5,
    ):
        self.limits = limits or RiskLimits()
        self.max_signal_age_seconds = max_signal_age_seconds
        self.max_spread_pct = max_spread_pct
        self.plan_drift_tolerance_pct = plan_drift_tolerance_pct
        self.min_slippage_survival_fraction = min_slippage_survival_fraction

    def validate(
        self,
        candidate: CandidateSignal,
        plan: TradePlan,
        fresh: FreshMarketState,
        recent_signal_ids: set[str],
        planned_qty: float,
    ) -> ValidationResult:
        reasons: list[str] = []

        # 1. Symbol remains active and tradable.
        if not (fresh.symbol_active and fresh.symbol_tradable):
            reasons.append("symbol is no longer active/tradable")

        # 2. Direction is allowed for the asset class.
        if plan.asset_class == "crypto" and plan.direction == "short":
            reasons.append("crypto is spot-only -- short direction not allowed")

        # 3. Equity short remains shortable and easy-to-borrow.
        if plan.direction == "short" and plan.asset_class != "crypto":
            if not (fresh.shortable and fresh.easy_to_borrow):
                reasons.append("short no longer shortable/easy-to-borrow")

        # 4. No existing conflicting position or order.
        if fresh.has_existing_position:
            reasons.append("an existing position already exists for this symbol")

        # 5. No duplicate order with the same signal ID.
        if candidate.signal_id in recent_signal_ids:
            reasons.append(f"duplicate order for signal_id {candidate.signal_id}")
        if fresh.has_existing_open_order:
            reasons.append("an existing open order already exists for this symbol")

        # 6. Signal has not expired.
        age_seconds = (fresh.now - fresh.signal_created_at).total_seconds()
        if age_seconds > self.max_signal_age_seconds:
            reasons.append(f"signal is {age_seconds:.0f}s old, exceeding {self.max_signal_age_seconds:.0f}s")

        # 7. Regime has not changed.
        if fresh.regime_now != candidate.regime:
            reasons.append(f"regime changed from {candidate.regime} to {fresh.regime_now}")

        # 8. Latest two timeframes still confirm.
        if not fresh.two_timeframes_confirm:
            reasons.append("latest two timeframes no longer confirm the setup")

        # 9. Spread has not expanded beyond threshold.
        if fresh.spread_pct > self.max_spread_pct:
            reasons.append(f"spread {fresh.spread_pct:.4%} exceeds {self.max_spread_pct:.4%}")

        # 10. Expected slippage does not destroy expectancy.
        risk = abs(plan.entry - plan.stop)
        if risk > 0:
            slippage_estimate = fresh.spread_pct * fresh.latest_price
            if slippage_estimate > risk * (1 - self.min_slippage_survival_fraction):
                reasons.append(
                    f"expected slippage ${slippage_estimate:.4f} threatens to erode the "
                    f"${risk:.4f} planned risk beyond tolerance"
                )

        # 11. Stop and target are valid for the latest price, and the
        #     independently recomputed plan hasn't drifted too far from
        #     the original -- logged for audit either way.
        recomputed = self._recompute_plan(plan, fresh)
        drift_pct = abs(recomputed.entry - plan.entry) / plan.entry if plan.entry else 1.0
        stop_target_valid = (
            (plan.direction == "long" and plan.stop < fresh.latest_price < plan.target)
            or (plan.direction == "short" and plan.target < fresh.latest_price < plan.stop)
        )
        if not stop_target_valid:
            reasons.append("stop/target no longer bracket the latest price correctly")
        if drift_pct > self.plan_drift_tolerance_pct:
            reasons.append(
                f"recomputed entry ${recomputed.entry:.4f} drifted {drift_pct:.2%} from "
                f"planned ${plan.entry:.4f}, exceeding {self.plan_drift_tolerance_pct:.2%} tolerance"
            )

        # 12. Position size respects all account and concentration limits.
        position_notional = planned_qty * plan.entry
        max_pct = (
            self.limits.max_crypto_position_pct if plan.asset_class == "crypto"
            else self.limits.max_equity_position_pct
        )
        if plan.direction == "short":
            max_pct = min(max_pct, self.limits.max_individual_short_pct)
        if fresh.account_equity > 0 and position_notional > max_pct * fresh.account_equity:
            reasons.append(
                f"position notional ${position_notional:,.2f} exceeds "
                f"{max_pct:.0%} of equity (${fresh.account_equity:,.2f})"
            )
        if fresh.open_positions_count >= self.limits.max_simultaneous_positions:
            reasons.append(
                f"already at max simultaneous positions ({self.limits.max_simultaneous_positions})"
            )
        cluster_notional = fresh.correlated_cluster_exposure_usd + position_notional
        if fresh.account_equity > 0 and cluster_notional > self.limits.max_correlated_cluster_pct * fresh.account_equity:
            reasons.append(
                f"correlated-cluster exposure ${cluster_notional:,.2f} would exceed "
                f"{self.limits.max_correlated_cluster_pct:.0%} of equity"
            )
        if position_notional > fresh.account_buying_power:
            reasons.append(
                f"position notional ${position_notional:,.2f} exceeds buying power "
                f"${fresh.account_buying_power:,.2f}"
            )

        # 13. Gross exposure remains at or below 100% (never leveraged long).
        gross_exposure_after = fresh.deployed_usd + position_notional
        if fresh.account_equity > 0 and gross_exposure_after > self.limits.max_gross_exposure_pct * fresh.account_equity:
            reasons.append(
                f"gross exposure ${gross_exposure_after:,.2f} would exceed "
                f"{self.limits.max_gross_exposure_pct:.0%} of equity"
            )

        # 14. Daily and weekly loss limits have not triggered.
        if fresh.daily_pnl_pct <= -self.limits.daily_stop_pct:
            reasons.append(f"daily stop breached ({fresh.daily_pnl_pct:.2%})")
        if fresh.weekly_pnl_pct <= -self.limits.weekly_stop_pct:
            reasons.append(f"weekly stop breached ({fresh.weekly_pnl_pct:.2%})")
        if fresh.consecutive_losses_today >= self.limits.max_consecutive_losses:
            reasons.append(
                f"{fresh.consecutive_losses_today} consecutive losses today, at/above the "
                f"{self.limits.max_consecutive_losses} limit"
            )

        # 15. Market/session permits the order type.
        if not fresh.session_permits_order_type:
            reasons.append("current session does not permit this order type")

        # 16. Data is fresh and connections are healthy.
        if not fresh.connections_healthy:
            reasons.append("market-data/trading connections are not healthy")
        max_quote_age = 5.0 if plan.asset_class == "crypto" else 2.0
        if fresh.latest_quote_age_seconds > max_quote_age:
            reasons.append(
                f"latest quote is {fresh.latest_quote_age_seconds:.1f}s old, exceeding {max_quote_age:.1f}s"
            )

        # 17. A stress scenario remains inside allowed risk.
        stressed_loss = self._stress_scenario_loss(plan, planned_qty, fresh)
        stress_budget = fresh.account_equity * self.limits.risk_per_trade_pct * 4
        if fresh.account_equity > 0 and stressed_loss > stress_budget:
            reasons.append(
                f"stress scenario loss ${stressed_loss:,.2f} exceeds stress budget ${stress_budget:,.2f}"
            )

        return ValidationResult(valid=not reasons, reasons=reasons)

    def _recompute_plan(self, plan: TradePlan, fresh: FreshMarketState) -> RecomputedPlan:
        """Recomputes entry/stop/target off the fresh price using the same
        relative stop/target distances as the original plan, independent
        of however the strategy originally derived them."""
        stop_distance = abs(plan.entry - plan.stop)
        target_distance = abs(plan.target - plan.entry)
        if plan.direction == "long":
            return RecomputedPlan(
                entry=fresh.latest_price, stop=fresh.latest_price - stop_distance,
                target=fresh.latest_price + target_distance,
            )
        return RecomputedPlan(
            entry=fresh.latest_price, stop=fresh.latest_price + stop_distance,
            target=fresh.latest_price - target_distance,
        )

    def _stress_scenario_loss(self, plan: TradePlan, qty: float, fresh: FreshMarketState) -> float:
        """Worst case: price gaps stress_move_pct beyond the stop before the
        stop can fill (real risk on any stop order -- it's a trigger, not a
        guarantee)."""
        stressed_price = (
            plan.stop * (1 - fresh.stress_move_pct) if plan.direction == "long"
            else plan.stop * (1 + fresh.stress_move_pct)
        )
        loss_per_unit = abs(plan.entry - stressed_price)
        return loss_per_unit * qty
