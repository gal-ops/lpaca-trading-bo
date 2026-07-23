"""Per-bucket probability model training + calibration (spec section 7).

Chronological walk-forward only -- examples are sorted by timestamp and
split into train/calibration/test windows in time order; the test window
is never touched during fitting or calibration (no random shuffling, no
look-ahead, no reusing the same samples for both selection and final
evaluation).

Base model: regularized logistic regression (spec's own first suggestion,
preferred over more complex models until they prove themselves out of
sample). Calibration: isotonic regression fit on the calibration window,
evaluated (never re-fit) on the untouched test window.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import datetime

from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

from alpaca_bot.models.features import union_feature_names, vectorize

MIN_EXAMPLES_DEFAULT = 200


@dataclass
class TrainingExample:
    bucket_key: str
    features: dict
    label: int          # 1 = net-profitable outcome after costs, 0 = not
    ts: datetime
    raw_score: float | None = None   # optional strategy-native score, unused by default


@dataclass
class TrainedBucketModel:
    bucket_key: str
    feature_names: list[str]
    coefficients: list[float]
    intercept: float
    calibration_x: list[float]      # fitted isotonic regression breakpoints (JSON-serializable)
    calibration_y: list[float]
    n_train: int
    n_calibration: int
    n_test: int
    test_brier_score: float
    test_calibration_error: float
    model_version: str = "v1_logreg_isotonic"


def chronological_walk_forward_split(
    examples: list[TrainingExample], train_frac: float = 0.6, calib_frac: float = 0.2,
) -> tuple[list[TrainingExample], list[TrainingExample], list[TrainingExample]]:
    ordered = sorted(examples, key=lambda e: e.ts)
    n = len(ordered)
    n_train = int(n * train_frac)
    n_calib = int(n * calib_frac)
    train = ordered[:n_train]
    calib = ordered[n_train:n_train + n_calib]
    test = ordered[n_train + n_calib:]
    return train, calib, test


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = 2.718281828459045 ** (-x)
        return 1 / (1 + z)
    z = 2.718281828459045 ** x
    return z / (1 + z)


def _isotonic_lookup(x: list[float], y: list[float], value: float) -> float:
    """Linear interpolation over the fitted isotonic breakpoints. Clips to
    the min/max calibrated value outside the observed range."""
    if not x:
        return value
    idx = bisect.bisect_left(x, value)
    if idx == 0:
        return y[0]
    if idx >= len(x):
        return y[-1]
    x0, x1 = x[idx - 1], x[idx]
    y0, y1 = y[idx - 1], y[idx]
    if x1 == x0:
        return y1
    frac = (value - x0) / (x1 - x0)
    return y0 + frac * (y1 - y0)


def calibrated_probability(model: TrainedBucketModel, features: dict) -> float:
    vector = vectorize(features, model.feature_names)
    z = model.intercept + sum(w * x for w, x in zip(model.coefficients, vector))
    raw_prob = _sigmoid(z)
    return _isotonic_lookup(model.calibration_x, model.calibration_y, raw_prob)


def train_bucket_model(
    examples: list[TrainingExample], min_examples: int = MIN_EXAMPLES_DEFAULT,
) -> TrainedBucketModel | None:
    """Returns None (caller must treat as RESEARCH_ONLY_INSUFFICIENT_SAMPLE)
    if there aren't enough examples for this bucket -- spec section 7's
    explicit floor of 200 out-of-sample examples."""
    if len(examples) < min_examples:
        return None

    train, calib, test = chronological_walk_forward_split(examples)
    if not train or not calib or not test:
        return None
    if len({e.label for e in train}) < 2:
        return None  # logistic regression needs both classes represented

    feature_names = union_feature_names([e.features for e in examples])
    if not feature_names:
        return None

    x_train = [vectorize(e.features, feature_names) for e in train]
    y_train = [e.label for e in train]
    base_model = LogisticRegression(C=1.0, max_iter=1000)  # l2-regularized by default
    base_model.fit(x_train, y_train)

    x_calib = [vectorize(e.features, feature_names) for e in calib]
    y_calib = [e.label for e in calib]
    raw_probs_calib = base_model.predict_proba(x_calib)[:, 1]

    isotonic = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    isotonic.fit(raw_probs_calib, y_calib)
    calibration_x = sorted(set(round(p, 6) for p in raw_probs_calib))
    calibration_y = list(isotonic.predict(calibration_x))

    trained = TrainedBucketModel(
        bucket_key=examples[0].bucket_key, feature_names=feature_names,
        coefficients=list(base_model.coef_[0]), intercept=float(base_model.intercept_[0]),
        calibration_x=[float(x) for x in calibration_x], calibration_y=[float(y) for y in calibration_y],
        n_train=len(train), n_calibration=len(calib), n_test=0,
        test_brier_score=0.0, test_calibration_error=0.0,
    )

    y_test = [e.label for e in test]
    calibrated_test_probs = [calibrated_probability(trained, e.features) for e in test]
    brier = sum((p - y) ** 2 for p, y in zip(calibrated_test_probs, y_test)) / len(y_test) if y_test else 0.0
    mean_predicted = sum(calibrated_test_probs) / len(calibrated_test_probs) if calibrated_test_probs else 0.0
    mean_observed = sum(y_test) / len(y_test) if y_test else 0.0
    calibration_error = abs(mean_predicted - mean_observed)

    trained.n_test = len(test)
    trained.test_brier_score = brier
    trained.test_calibration_error = calibration_error
    return trained
