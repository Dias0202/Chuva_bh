"""Hiperparâmetros e parâmetros de avaliação compartilhados entre treino local e SageMaker."""

from __future__ import annotations

DEFAULT_TEST_YEAR = 2025
DEFAULT_FEATURE_SET = "full"

HYPERPARAMETERS: dict[str, str | int | float] = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "num_round": 600,
    "early_stopping_rounds": 40,
    "eta": 0.05,
    "max_depth": 5,
    "min_child_weight": 5,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
}
