"""Compara o modelo com as baselines nos mesmos dados de teste."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from features.build import LABEL
from training.baselines import ForecastBaseline, PersistenceBaseline
from training.metrics import best_threshold, brier_skill, reliability, summarize

BASELINES = ("persistence", "forecast")


def _score(
    validation: pd.DataFrame,
    test: pd.DataFrame,
    p_validation: np.ndarray,
    p_test: np.ndarray,
) -> dict[str, Any]:
    threshold = best_threshold(validation[LABEL], p_validation)
    result = summarize(test[LABEL], p_test, threshold)
    result["by_horizon"] = {
        str(int(h)): summarize(test.loc[mask, LABEL], p_test[mask.to_numpy()], threshold)
        for h, mask in ((h, test["horizon_h"] == h) for h in sorted(test["horizon_h"].unique()))
    }
    result["reliability"] = reliability(test[LABEL], p_test)
    return result


def compare(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    model_validation: np.ndarray,
    model_test: np.ndarray,
) -> dict[str, Any]:
    """Relatório usado pelo ConditionStep: o modelo precisa vencer a melhor baseline.

    As baselines são ajustadas no treino e todos os limiares são escolhidos na validação.
    """
    persistence = PersistenceBaseline().fit(train)
    forecast = ForecastBaseline().fit(train)
    predictions = {
        "model": (np.asarray(model_validation), np.asarray(model_test)),
        "persistence": (persistence.predict(validation), persistence.predict(test)),
        "forecast": (forecast.predict(validation), forecast.predict(test)),
    }
    metrics = {
        name: _score(validation, test, p_val, p_test)
        for name, (p_val, p_test) in predictions.items()
    }
    best_name = min(BASELINES, key=lambda name: metrics[name]["brier"])
    best_brier = metrics[best_name]["brier"]
    model_brier = metrics["model"]["brier"]
    return {
        "metrics": metrics,
        "best_baseline": {"name": best_name, "brier": best_brier},
        "brier_skill_vs_best_baseline": brier_skill(model_brier, best_brier),
        "model_beats_baselines": bool(model_brier < best_brier),
        "baselines": {"persistence": persistence.to_dict(), "forecast": forecast.to_dict()},
    }
