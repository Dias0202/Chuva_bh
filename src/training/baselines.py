"""Baselines calibradas no treino: persistência e previsão operacional."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from features.build import LABEL, RAIN_THRESHOLD_MM

# Faixas de chuva prevista (mm) usadas para calibrar a previsão operacional.
FORECAST_BIN_EDGES = (0.0, 0.1, 0.5, 1.0, 2.0, 5.0)
# Suavização de Laplace: evita probabilidade 0 ou 1 em faixas com poucas amostras.
_PRIOR_STRENGTH = 1.0


class _BinnedFrequency:
    """Frequência observada de chuva por horizonte e faixa de uma variável."""

    def __init__(self) -> None:
        self.rates: dict[tuple[int, int], float] = {}
        self.fallback: dict[int, float] = {}

    def _bins(self, frame: pd.DataFrame) -> np.ndarray:
        raise NotImplementedError

    def fit(self, frame: pd.DataFrame) -> _BinnedFrequency:
        if frame.empty:
            raise ValueError("treino vazio")
        data = pd.DataFrame(
            {"h": frame["horizon_h"].to_numpy(), "b": self._bins(frame), "y": frame[LABEL]}
        )
        for horizon, group in data.groupby("h"):
            base = float(group["y"].mean())
            self.fallback[int(horizon)] = base
            for bin_id, cell in group.groupby("b"):
                if bin_id < 0:
                    continue
                rate = (cell["y"].sum() + _PRIOR_STRENGTH * base) / (len(cell) + _PRIOR_STRENGTH)
                self.rates[(int(horizon), int(bin_id))] = float(rate)
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        if not self.fallback:
            raise RuntimeError("baseline não ajustada")
        overall = float(np.mean(list(self.fallback.values())))
        result = []
        # zip(strict=) não existe no Python 3.9 dos containers do SageMaker.
        for horizon, bin_id in zip(frame["horizon_h"].to_numpy(), self._bins(frame)):  # noqa: B905
            default = self.fallback.get(int(horizon), overall)
            result.append(self.rates.get((int(horizon), int(bin_id)), default))
        return np.asarray(result, dtype=float)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates": {f"{h}:{b}": r for (h, b), r in sorted(self.rates.items())},
            "fallback": {str(h): r for h, r in sorted(self.fallback.items())},
        }


class PersistenceBaseline(_BinnedFrequency):
    """P(chuva em t+h | choveu ou não em t), estimada no treino."""

    def _bins(self, frame: pd.DataFrame) -> np.ndarray:
        rain = frame["precip_mm"].to_numpy(dtype=float)
        return np.where(np.isnan(rain), -1, (rain >= RAIN_THRESHOLD_MM).astype(int))


class ForecastBaseline(_BinnedFrequency):
    """Chuva prevista pela Open-Meteo para t+h, convertida em probabilidade por faixa."""

    def _bins(self, frame: pd.DataFrame) -> np.ndarray:
        rain = frame["fc_precipitation_mm"].to_numpy(dtype=float)
        bins = np.digitize(np.nan_to_num(rain, nan=0.0), FORECAST_BIN_EDGES[1:], right=True)
        return np.where(np.isnan(rain), -1, bins)
