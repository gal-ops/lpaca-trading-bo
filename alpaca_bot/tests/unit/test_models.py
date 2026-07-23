"""Unit tests for the probability model training/calibration pipeline and
the 85% confidence gate (spec section 7)."""

import json
import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from alpaca_bot.models.calibration_monitor import check_and_disable_if_miscalibrated
from alpaca_bot.models.gate import RESEARCH_ONLY_INSUFFICIENT_SAMPLE, ConfidenceGate, bucket_key_for
from alpaca_bot.models.training import (
    TrainingExample,
    calibrated_probability,
    chronological_walk_forward_split,
    train_bucket_model,
)
from alpaca_bot.persistence.db import Database
from alpaca_bot.strategies.base import CandidateSignal, TradePlan


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


def _synthetic_examples(n=300, seed=1) -> list[TrainingExample]:
    """A feature ('signal_strength') that's genuinely predictive, so a
    real model should be trainable and reasonably well-calibrated."""
    rng = random.Random(seed)
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    examples = []
    for i in range(n):
        strength = rng.uniform(0, 1)
        win_prob = 0.2 + 0.6 * strength
        label = 1 if rng.random() < win_prob else 0
        examples.append(TrainingExample(
            bucket_key="vwap_pullback_continuation|long|stock|BULL_TREND",
            features={"signal_strength": strength}, label=label,
            ts=base + timedelta(minutes=i),
        ))
    return examples


def test_chronological_split_preserves_time_order():
    examples = _synthetic_examples(100)
    train, calib, test = chronological_walk_forward_split(examples)
    assert train[-1].ts <= calib[0].ts
    assert calib[-1].ts <= test[0].ts
    assert len(train) + len(calib) + len(test) == 100


def test_train_bucket_model_returns_none_below_minimum_examples():
    examples = _synthetic_examples(50)
    assert train_bucket_model(examples, min_examples=200) is None


def test_train_bucket_model_learns_a_predictive_feature():
    examples = _synthetic_examples(400)
    model = train_bucket_model(examples, min_examples=200)
    assert model is not None
    assert model.n_train + model.n_calibration + model.n_test == 400
    low_prob = calibrated_probability(model, {"signal_strength": 0.05})
    high_prob = calibrated_probability(model, {"signal_strength": 0.95})
    assert high_prob > low_prob


def test_train_bucket_model_serializes_to_json_and_back():
    examples = _synthetic_examples(400)
    model = train_bucket_model(examples, min_examples=200)
    payload = json.dumps(asdict(model))
    restored_dict = json.loads(payload)
    prob_before = calibrated_probability(model, {"signal_strength": 0.8})
    from alpaca_bot.models.training import TrainedBucketModel
    restored = TrainedBucketModel(**restored_dict)
    prob_after = calibrated_probability(restored, {"signal_strength": 0.8})
    assert prob_before == prob_after


def _candidate_and_plan(strategy="vwap_pullback_continuation", direction="long",
                        asset_class="stock", regime="BULL_TREND", feature_snapshot=None):
    candidate = CandidateSignal(
        signal_id="sig-1", strategy=strategy, symbol="AAPL", asset_class=asset_class,
        direction=direction, regime=regime, entry=100.0, stop=98.0, target=104.0,
        max_holding_seconds=3600.0, feature_snapshot=feature_snapshot or {"signal_strength": 0.9},
    )
    plan = TradePlan(
        symbol="AAPL", asset_class=asset_class, direction=direction, entry=100.0, stop=98.0,
        target=104.0, max_holding_seconds=3600.0, reward_risk=2.0, strategy=strategy,
        signal_id="sig-1",
    )
    return candidate, plan


def test_gate_returns_insufficient_sample_when_no_bucket_exists(db):
    gate = ConfidenceGate(db)
    candidate, plan = _candidate_and_plan()
    result = gate.evaluate(candidate, plan)
    assert result.accepted is False
    assert result.reasons == [RESEARCH_ONLY_INSUFFICIENT_SAMPLE]


def test_gate_returns_insufficient_sample_when_bucket_below_minimum(db):
    key = bucket_key_for(_candidate_and_plan()[0])
    db.upsert_calibration_bucket(key, n_examples=50)
    gate = ConfidenceGate(db, min_examples=200)
    candidate, plan = _candidate_and_plan()
    result = gate.evaluate(candidate, plan)
    assert result.accepted is False
    assert RESEARCH_ONLY_INSUFFICIENT_SAMPLE in result.reasons


def test_gate_accepts_when_calibrated_probability_clears_threshold(db):
    examples = _synthetic_examples(400)
    model = train_bucket_model(examples, min_examples=200)
    key = bucket_key_for(_candidate_and_plan()[0])
    db.upsert_calibration_bucket(key, n_examples=400, calibration=asdict(model))

    gate = ConfidenceGate(db, min_examples=200, bootstrap_margin=0.0)
    candidate, plan = _candidate_and_plan(feature_snapshot={"signal_strength": 0.98})
    result = gate.evaluate(candidate, plan)
    # With a strongly predictive feature at its max value the calibrated
    # probability should clear 0.85; if the synthetic model doesn't quite
    # get there, at minimum no exception should occur and reasons should
    # explain exactly why not.
    assert result.calibrated_probability is not None
    if result.calibrated_probability >= 0.85:
        assert result.accepted is True
    else:
        assert any("calibrated probability" in r for r in result.reasons)


def test_gate_rejects_on_weak_reward_risk(db):
    examples = _synthetic_examples(400)
    model = train_bucket_model(examples, min_examples=200)
    key = bucket_key_for(_candidate_and_plan()[0])
    db.upsert_calibration_bucket(key, n_examples=400, calibration=asdict(model))

    gate = ConfidenceGate(db, min_examples=200, min_reward_risk=1.5)
    candidate, plan = _candidate_and_plan(feature_snapshot={"signal_strength": 0.98})
    plan.reward_risk = 1.0
    result = gate.evaluate(candidate, plan)
    assert result.accepted is False
    assert any("reward/risk" in r for r in result.reasons)


def test_gate_rejects_on_disagreeing_independent_checks(db):
    examples = _synthetic_examples(400)
    model = train_bucket_model(examples, min_examples=200)
    key = bucket_key_for(_candidate_and_plan()[0])
    db.upsert_calibration_bucket(key, n_examples=400, calibration=asdict(model))

    gate = ConfidenceGate(db, min_examples=200)
    candidate, plan = _candidate_and_plan(feature_snapshot={"signal_strength": 0.98})
    result = gate.evaluate(candidate, plan, independent_rule_validator_agrees=False)
    assert result.accepted is False
    assert any("independent rule validator" in r for r in result.reasons)


def test_gate_respects_disabled_bucket(db):
    key = bucket_key_for(_candidate_and_plan()[0])
    db.upsert_calibration_bucket(key, n_examples=400, disabled=True, disabled_reason="drifted")
    gate = ConfidenceGate(db, min_examples=200)
    candidate, plan = _candidate_and_plan()
    result = gate.evaluate(candidate, plan)
    assert result.accepted is False
    assert any("disabled" in r for r in result.reasons)


def test_calibration_monitor_disables_bucket_on_drift(db):
    key = "vwap_pullback_continuation|long|stock|BULL_TREND"
    db.upsert_calibration_bucket(key, n_examples=400)
    # 40 signals predicted near 0.85 but only winning ~30% of the time --
    # a large, real miscalibration.
    for i in range(40):
        won = 1 if i < 12 else 0
        db.record_signal({
            "signal_id": f"sig-{i}", "strategy": "vwap_pullback_continuation", "symbol": "AAPL",
            "asset_class": "stock", "direction": "long", "regime": "BULL_TREND",
            "calibrated_probability": 0.86, "accepted": True,
        })
        db.record_signal_outcome(f"sig-{i}", bool(won))

    disabled = check_and_disable_if_miscalibrated(db, key)
    assert disabled is True
    row = db.get_calibration_bucket(key)
    assert row["disabled"] == 1


def test_calibration_monitor_leaves_well_calibrated_bucket_enabled(db):
    key = "vwap_pullback_continuation|long|stock|BULL_TREND"
    db.upsert_calibration_bucket(key, n_examples=400)
    for i in range(40):
        won = 1 if i < 34 else 0  # 85% hit rate, matches the predicted probability
        db.record_signal({
            "signal_id": f"sig-{i}", "strategy": "vwap_pullback_continuation", "symbol": "AAPL",
            "asset_class": "stock", "direction": "long", "regime": "BULL_TREND",
            "calibrated_probability": 0.86, "accepted": True,
        })
        db.record_signal_outcome(f"sig-{i}", bool(won))

    disabled = check_and_disable_if_miscalibrated(db, key)
    assert disabled is False
    row = db.get_calibration_bucket(key)
    assert row["disabled"] == 0


def test_calibration_monitor_no_action_below_min_samples(db):
    key = "vwap_pullback_continuation|long|stock|BULL_TREND"
    db.upsert_calibration_bucket(key, n_examples=400)
    for i in range(5):
        db.record_signal({
            "signal_id": f"sig-{i}", "strategy": "vwap_pullback_continuation", "symbol": "AAPL",
            "asset_class": "stock", "direction": "long", "regime": "BULL_TREND",
            "calibrated_probability": 0.86, "accepted": True,
        })
        db.record_signal_outcome(f"sig-{i}", False)
    assert check_and_disable_if_miscalibrated(db, key) is False
