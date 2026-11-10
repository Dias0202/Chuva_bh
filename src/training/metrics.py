"""Métricas probabilísticas e de contingência, sem dependências além do numpy."""

from __future__ import annotations

from typing import Any

import numpy as np

THRESHOLD_GRID = tuple(round(0.05 * i, 2) for i in range(1, 20))


def _arrays(y_true: Any, y_prob: Any) -> tuple[np.ndarray, np.ndarray]:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_prob, dtype=float)
    if y.shape != p.shape or y.ndim != 1:
        raise ValueError("y_true e y_prob devem ser vetores do mesmo tamanho")
    if y.size == 0:
        raise ValueError("sem amostras")
    if np.isnan(p).any() or ((p < 0) | (p > 1)).any():
        raise ValueError("probabilidades devem estar em [0, 1]")
    return y, p


def brier_score(y_true: Any, y_prob: Any) -> float:
    y, p = _arrays(y_true, y_prob)
    return float(np.mean((p - y) ** 2))


def average_precision(y_true: Any, y_prob: Any) -> float:
    """Área sob a curva precisão-revocação (mesma definição do scikit-learn)."""
    y, p = _arrays(y_true, y_prob)
    positives = y.sum()
    if positives == 0:
        return float("nan")
    order = np.argsort(-p, kind="mergesort")
    y_sorted, p_sorted = y[order], p[order]
    # Considera empates: só fecha um ponto da curva quando a probabilidade muda.
    distinct = np.where(np.diff(p_sorted))[0]
    cut = np.r_[distinct, y.size - 1]
    tp = np.cumsum(y_sorted)[cut]
    predicted = cut + 1
    precision = tp / predicted
    recall = tp / positives
    recall_steps = np.diff(np.r_[0.0, recall])
    return float(np.sum(recall_steps * precision))


def contingency(y_true: Any, y_prob: Any, threshold: float) -> dict[str, float]:
    """POD, FAR e CSI, as medidas usuais de verificação de previsão de chuva."""
    y, p = _arrays(y_true, y_prob)
    predicted = p >= threshold
    observed = y >= 0.5
    hits = float(np.sum(predicted & observed))
    misses = float(np.sum(~predicted & observed))
    false_alarms = float(np.sum(predicted & ~observed))
    pod = hits / (hits + misses) if hits + misses else float("nan")
    far = false_alarms / (hits + false_alarms) if hits + false_alarms else float("nan")
    denominator = hits + misses + false_alarms
    csi = hits / denominator if denominator else float("nan")
    return {"pod": pod, "far": far, "csi": csi}


def best_threshold(y_true: Any, y_prob: Any) -> float:
    """Limiar que maximiza o CSI. Escolher na validação, nunca no teste."""
    scores = [(contingency(y_true, y_prob, t)["csi"], t) for t in THRESHOLD_GRID]
    valid = [(s, t) for s, t in scores if not np.isnan(s)]
    return max(valid)[1] if valid else 0.5


def reliability(y_true: Any, y_prob: Any, bins: int = 10) -> list[dict[str, float]]:
    """Curva de confiabilidade: probabilidade média prevista contra frequência observada."""
    y, p = _arrays(y_true, y_prob)
    edges = np.linspace(0.0, 1.0, bins + 1)
    index = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    curve = []
    for b in range(bins):
        mask = index == b
        if mask.any():
            curve.append(
                {
                    "mean_predicted": float(p[mask].mean()),
                    "observed_frequency": float(y[mask].mean()),
                    "count": float(mask.sum()),
                }
            )
    return curve


def summarize(y_true: Any, y_prob: Any, threshold: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "brier": brier_score(y_true, y_prob),
        "pr_auc": average_precision(y_true, y_prob),
        "threshold": threshold,
        "samples": int(np.asarray(y_true).size),
        "positive_rate": float(np.mean(np.asarray(y_true, dtype=float))),
    }
    result.update(contingency(y_true, y_prob, threshold))
    return result


def brier_skill(model_brier: float, reference_brier: float) -> float:
    """Fração de melhora sobre a referência: positivo é melhor."""
    if reference_brier == 0:
        return float("nan")
    return 1.0 - model_brier / reference_brier
