"""Calibration drift monitor (spec section 7): "Track calibration tables.
If signals predicted near 85% do not win near their expected frequency,
automatically disable that model bucket."

Requires `outcome_label` on real accepted signals, which execution
(phase 10) fills in once a trade closes -- this module only reads that
history and decides whether a bucket's calibration has drifted enough to
warrant disabling it. It never re-enables a bucket automatically;
re-enabling after investigation is a deliberate human action.
"""

from __future__ import annotations

import json

from alpaca_bot.persistence.db import Database

MIN_SAMPLES_TO_JUDGE = 30
DEFAULT_TOLERANCE = 0.15   # observed hit-rate may drift this far from the predicted probability


def check_and_disable_if_miscalibrated(
    db: Database,
    bucket_key: str,
    target_probability: float = 0.85,
    probability_window: float = 0.05,
    tolerance: float = DEFAULT_TOLERANCE,
    min_samples: int = MIN_SAMPLES_TO_JUDGE,
) -> bool:
    """Returns True if the bucket was (or already was) disabled."""
    rows = db.bucket_outcomes_near_probability(bucket_key, target_probability, probability_window)
    if len(rows) < min_samples:
        return False  # not enough evidence yet either way

    observed_hit_rate = sum(r["outcome_label"] for r in rows) / len(rows)
    drift = abs(observed_hit_rate - target_probability)
    if drift <= tolerance:
        return False

    row = db.get_calibration_bucket(bucket_key)
    n_examples = row["n_examples"] if row else 0
    model_version = row["model_version"] if row else None
    db.upsert_calibration_bucket(
        bucket_key, n_examples=n_examples, model_version=model_version,
        calibration=_safe_calibration(row), disabled=True,
        disabled_reason=(
            f"observed hit rate {observed_hit_rate:.2%} over {len(rows)} signals near "
            f"{target_probability:.0%} predicted, drift {drift:.2%} exceeds tolerance {tolerance:.2%}"
        ),
    )
    return True


def _safe_calibration(row) -> dict:
    if row is None or not row["calibration_json"]:
        return {}
    return json.loads(row["calibration_json"])
