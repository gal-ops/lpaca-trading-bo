#!/usr/bin/env python3
"""Trains/calibrates a probability model for every bucket that has
enough real, outcome-labeled signals in the database, and stores the
result in calibration_buckets.

On a brand-new account with no trade history, this will (correctly)
report that every bucket has 0 examples and train nothing -- see spec
section 7: the confidence gate must never fire on an untrained/
insufficiently-sampled bucket, so there is nothing dishonest about this
script doing nothing on day one. Run it periodically as real signal
outcomes accumulate.
"""

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alpaca_bot.config import get_settings, load_strategy_config  # noqa: E402
from alpaca_bot.models.training import TrainingExample, train_bucket_model  # noqa: E402
from alpaca_bot.persistence.db import Database  # noqa: E402


def main() -> int:
    settings = get_settings()
    cfg = load_strategy_config()
    min_examples = cfg["confidence_gate"]["min_out_of_sample_examples"]

    db = Database(settings.database_path)
    try:
        rows = db.query("""
            SELECT strategy, direction, asset_class, regime, feature_snapshot_json,
                   outcome_label, ts
            FROM signals
            WHERE accepted = 1 AND outcome_label IS NOT NULL
        """)
        buckets: dict[str, list[TrainingExample]] = {}
        for row in rows:
            key = f"{row['strategy']}|{row['direction']}|{row['asset_class']}|{row['regime']}"
            features = json.loads(row["feature_snapshot_json"]) if row["feature_snapshot_json"] else {}
            buckets.setdefault(key, []).append(TrainingExample(
                bucket_key=key, features=features, label=row["outcome_label"],
                ts=datetime.fromisoformat(row["ts"]),
            ))

        if not buckets:
            print("No outcome-labeled signals yet -- nothing to train. This is expected "
                  "on a new account; the confidence gate will report "
                  "RESEARCH_ONLY_INSUFFICIENT_SAMPLE for every signal until real trade "
                  "history accumulates.")
            return 0

        for key, examples in buckets.items():
            if len(examples) < min_examples:
                print(f"{key}: {len(examples)} examples, below the {min_examples} minimum -- skipping.")
                continue
            model = train_bucket_model(examples, min_examples=min_examples)
            if model is None:
                print(f"{key}: training failed (e.g. only one outcome class present) -- skipping.")
                continue
            db.upsert_calibration_bucket(
                key, n_examples=len(examples), model_version=model.model_version,
                calibration=asdict(model),
            )
            print(f"{key}: trained on {len(examples)} examples "
                  f"(test Brier={model.test_brier_score:.4f}, "
                  f"calibration error={model.test_calibration_error:.4f})")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
