"""Population Stability Index por feature, com faixas definidas pelos quantis da referência."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

import numpy as np
import pandas as pd

BINS: Final = 10
# Evita log(0) em faixas vazias, sem mudar o resultado de forma relevante.
EPSILON: Final = 1e-4
DRIFT_THRESHOLD: Final = 0.25
MIN_SAMPLES: Final = 48


def reference_bins(values: Iterable[float], bins: int = BINS) -> dict[str, list[float]]:
    data = np.asarray([v for v in values if v is not None and not np.isnan(v)], dtype=float)
    if data.size < MIN_SAMPLES:
        raise ValueError("referência com poucas amostras")
    inner = np.unique(np.quantile(data, np.linspace(0, 1, bins + 1)[1:-1]))
    counts = np.bincount(np.searchsorted(inner, data, side="right"), minlength=inner.size + 1)
    return {"edges": inner.tolist(), "proportions": (counts / counts.sum()).tolist()}


def psi(reference: Mapping[str, list[float]], values: Iterable[float]) -> float | None:
    """None quando a janela atual tem poucas amostras para uma comparação estável."""
    data = np.asarray([v for v in values if v is not None and not np.isnan(v)], dtype=float)
    if data.size < MIN_SAMPLES:
        return None
    edges = np.asarray(reference["edges"], dtype=float)
    expected = np.asarray(reference["proportions"], dtype=float)
    counts = np.bincount(np.searchsorted(edges, data, side="right"), minlength=expected.size)
    actual = counts / counts.sum()
    expected = np.clip(expected, EPSILON, None)
    actual = np.clip(actual, EPSILON, None)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


def build_reference(frame: pd.DataFrame, features: Iterable[str]) -> dict[str, Any]:
    return {feature: reference_bins(frame[feature].to_numpy(dtype=float)) for feature in features}


def drift_scores(reference: Mapping[str, Any], frame: pd.DataFrame) -> dict[str, float]:
    scores = {}
    for feature, bins in reference.items():
        if feature in frame.columns:
            value = psi(bins, frame[feature].to_numpy(dtype=float))
            if value is not None:
                scores[feature] = value
    return scores
