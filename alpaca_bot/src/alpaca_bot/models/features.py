"""Turns a strategy's feature_snapshot dict into a fixed-order numeric
vector for the probability models. Missing/non-numeric keys become 0.0
rather than raising -- different strategies populate different keys, and
a bucket's feature set is defined by whatever the union of its training
examples actually contains."""

from __future__ import annotations


def vectorize(feature_snapshot: dict, feature_names: list[str]) -> list[float]:
    vector = []
    for name in feature_names:
        value = feature_snapshot.get(name)
        if value is None:
            vector.append(0.0)
            continue
        try:
            vector.append(float(value))
        except (TypeError, ValueError):
            vector.append(0.0)
    return vector


def union_feature_names(feature_snapshots: list[dict]) -> list[str]:
    names: set[str] = set()
    for snapshot in feature_snapshots:
        for key, value in snapshot.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                names.add(key)
    return sorted(names)
