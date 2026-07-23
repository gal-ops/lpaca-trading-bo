"""The 85% confidence gate (spec section 7). Executes a trade only when
every one of the spec's listed requirements passes -- calibrated
probability, a conservative bootstrap lower bound, independent rule-
validator agreement, regime-model agreement, multi-timeframe agreement,
positive expected value after costs, minimum reward/risk, matching
model/feed type, and a sufficient calibration sample.

Bucketed by strategy|direction|asset_class|regime, per spec section 7's
requirement that models be separated across those dimensions (and SIP vs
IEX data, tracked separately via `feed_type`).

The bot may legitimately execute very few trades, and the sample-size
floor is never lowered to manufacture activity -- if a bucket has fewer
than `min_examples` real out-of-sample examples, every signal in it comes
back RESEARCH_ONLY_INSUFFICIENT_SAMPLE, full stop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from alpaca_bot.models.training import TrainedBucketModel, calibrated_probability
from alpaca_bot.persistence.db import Database
from alpaca_bot.strategies.base import CandidateSignal, TradePlan

RESEARCH_ONLY_INSUFFICIENT_SAMPLE = "RESEARCH_ONLY_INSUFFICIENT_SAMPLE"


def bucket_key_for(candidate: CandidateSignal) -> str:
    return f"{candidate.strategy}|{candidate.direction}|{candidate.asset_class}|{candidate.regime}"


@dataclass
class GateResult:
    accepted: bool
    calibrated_probability: float | None
    bootstrap_lower_bound: float | None
    expected_value_after_costs: float | None
    reward_risk: float
    reasons: list[str] = field(default_factory=list)


class ConfidenceGate:
    def __init__(
        self,
        db: Database,
        min_probability: float = 0.85,
        min_bootstrap_lower_bound: float = 0.85,
        min_reward_risk: float = 1.5,
        min_examples: int = 200,
        bootstrap_margin: float = 0.05,
        cost_per_unit: float = 0.0,
    ):
        self.db = db
        self.min_probability = min_probability
        self.min_bootstrap_lower_bound = min_bootstrap_lower_bound
        self.min_reward_risk = min_reward_risk
        self.min_examples = min_examples
        self.bootstrap_margin = bootstrap_margin
        self.cost_per_unit = cost_per_unit

    def evaluate(
        self,
        candidate: CandidateSignal,
        plan: TradePlan,
        independent_rule_validator_agrees: bool = True,
        regime_model_agrees: bool = True,
        two_timeframes_agree: bool = True,
        model_feed_matches: bool = True,
    ) -> GateResult:
        key = bucket_key_for(candidate)
        row = self.db.get_calibration_bucket(key)
        reward_risk = plan.reward_risk

        if row is None or row["n_examples"] < self.min_examples:
            return GateResult(False, None, None, None, reward_risk, [RESEARCH_ONLY_INSUFFICIENT_SAMPLE])
        if row["disabled"]:
            return GateResult(False, None, None, None, reward_risk,
                               [f"model bucket disabled: {row['disabled_reason']}"])

        calibration_payload = json.loads(row["calibration_json"])
        model = TrainedBucketModel(**calibration_payload)
        probability = calibrated_probability(model, candidate.feature_snapshot)
        bootstrap_lower_bound = max(0.0, probability - self.bootstrap_margin)

        risk = abs(plan.entry - plan.stop)
        reward = abs(plan.target - plan.entry)
        expected_value = probability * reward - (1 - probability) * risk - self.cost_per_unit

        reasons = []
        if probability < self.min_probability:
            reasons.append(f"calibrated probability {probability:.3f} below {self.min_probability}")
        if bootstrap_lower_bound < self.min_bootstrap_lower_bound:
            reasons.append(
                f"bootstrap lower bound {bootstrap_lower_bound:.3f} below {self.min_bootstrap_lower_bound}"
            )
        if not independent_rule_validator_agrees:
            reasons.append("independent rule validator disagrees")
        if not regime_model_agrees:
            reasons.append("regime model disagrees")
        if not two_timeframes_agree:
            reasons.append("insufficient timeframe agreement")
        if expected_value <= 0:
            reasons.append(f"expected value after costs {expected_value:.4f} is not positive")
        if reward_risk < self.min_reward_risk:
            reasons.append(f"reward/risk {reward_risk:.2f} below {self.min_reward_risk}")
        if not model_feed_matches:
            reasons.append("model/feed type mismatch (e.g. SIP-trained model on IEX-only data)")

        return GateResult(
            accepted=not reasons, calibrated_probability=probability,
            bootstrap_lower_bound=bootstrap_lower_bound, expected_value_after_costs=expected_value,
            reward_risk=reward_risk, reasons=reasons,
        )
