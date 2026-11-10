"""Features e rótulos a partir de séries horárias.

Todas as funções são puras. O instante de emissão `issued_at` é a hora da última
observação disponível; nenhuma feature usa observação posterior a ele.

O código roda também nos containers do SageMaker, que usam Python 3.9: a sintaxe
fica restrita a essa versão (verificado em tests/test_features.py).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

HORIZONS = (1, 3, 6)
RAIN_THRESHOLD_MM = 0.5
LABEL = "label"
KEY_COLUMNS = ("point_id", "issued_at", "horizon_h")

OBSERVATION_FEATURES = (
    "precip_mm",
    "precip_lag1_mm",
    "precip_lag2_mm",
    "precip_sum_3h_mm",
    "precip_sum_6h_mm",
    "precip_sum_24h_mm",
    "temperature_c",
    "temperature_change_3h_c",
    "dew_point_depression_c",
    "relative_humidity_pct",
    "humidity_change_3h_pct",
    "pressure_hpa",
    "pressure_tendency_3h_hpa",
    "wind_speed_ms",
    "wind_gust_ms",
    "wind_dir_sin",
    "wind_dir_cos",
    "radiation_kj_m2",
)
CALENDAR_FEATURES = ("hour_sin", "hour_cos", "doy_sin", "doy_cos")
FORECAST_FEATURES = (
    "fc_precipitation_mm",
    "fc_precipitation_sum_mm",
    "fc_cape_j_kg",
    "fc_cloud_cover_pct",
    "fc_relative_humidity_pct",
)

FEATURE_SETS = {
    "full": ("horizon_h", *OBSERVATION_FEATURES, *CALENDAR_FEATURES, *FORECAST_FEATURES),
    "obs_only": ("horizon_h", *OBSERVATION_FEATURES, *CALENDAR_FEATURES),
}


def _regular(group: pd.DataFrame, time_column: str) -> pd.DataFrame:
    """Indexa por hora, sem lacunas no índice: horas ausentes viram NaN."""
    frame = group.sort_values(time_column).drop_duplicates(time_column, keep="last")
    return frame.set_index(time_column).asfreq("h")


def observation_features(observations: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for point_id, group in observations.groupby("point_id", sort=True):
        g = _regular(group, "observed_at")
        rain = g["precipitation_mm"]
        humidity = g["relative_humidity_pct"]
        temperature = g["temperature_c"]
        pressure = g["pressure_hpa"]
        direction = np.deg2rad(g["wind_direction_deg"])

        f = pd.DataFrame(index=g.index)
        f["precip_mm"] = rain
        f["precip_lag1_mm"] = rain.shift(1)
        f["precip_lag2_mm"] = rain.shift(2)
        f["precip_sum_3h_mm"] = rain.rolling(3, min_periods=3).sum()
        f["precip_sum_6h_mm"] = rain.rolling(6, min_periods=5).sum()
        f["precip_sum_24h_mm"] = rain.rolling(24, min_periods=20).sum()
        f["temperature_c"] = temperature
        f["temperature_change_3h_c"] = temperature - temperature.shift(3)
        f["dew_point_depression_c"] = temperature - g["dew_point_c"]
        f["relative_humidity_pct"] = humidity
        f["humidity_change_3h_pct"] = humidity - humidity.shift(3)
        f["pressure_hpa"] = pressure
        f["pressure_tendency_3h_hpa"] = pressure - pressure.shift(3)
        f["wind_speed_ms"] = g["wind_speed_ms"]
        f["wind_gust_ms"] = g["wind_gust_ms"]
        f["wind_dir_sin"] = np.sin(direction)
        f["wind_dir_cos"] = np.cos(direction)
        f["radiation_kj_m2"] = g["radiation_kj_m2"]
        f.index.name = "issued_at"
        f = f.reset_index()
        f["point_id"] = str(point_id)
        parts.append(f)
    return pd.concat(parts, ignore_index=True)


def calendar_features(issued_at: pd.Series) -> pd.DataFrame:
    hour = issued_at.dt.hour.astype(float)
    day = issued_at.dt.dayofyear.astype(float)
    return pd.DataFrame(
        {
            "hour_sin": np.sin(2 * np.pi * hour / 24),
            "hour_cos": np.cos(2 * np.pi * hour / 24),
            "doy_sin": np.sin(2 * np.pi * day / 365.25),
            "doy_cos": np.cos(2 * np.pi * day / 365.25),
        },
        index=issued_at.index,
    )


def forecast_features(forecasts: pd.DataFrame, horizons: Sequence[int]) -> pd.DataFrame:
    """Previsão válida em t+h e chuva prevista acumulada de t+1 a t+h.

    Espera no máximo uma previsão por `valid_at` e ponto: quem chama escolhe a rodada.
    """
    parts = []
    for point_id, group in forecasts.groupby("point_id", sort=True):
        g = _regular(group, "valid_at")
        rain = g["precipitation_mm"]
        for horizon in horizons:
            f = pd.DataFrame(index=g.index)
            f["fc_precipitation_mm"] = rain.shift(-horizon)
            f["fc_precipitation_sum_mm"] = (
                rain.rolling(horizon, min_periods=horizon).sum().shift(-horizon)
            )
            f["fc_cape_j_kg"] = g["cape_j_kg"].shift(-horizon)
            f["fc_cloud_cover_pct"] = g["cloud_cover_pct"].shift(-horizon)
            f["fc_relative_humidity_pct"] = g["relative_humidity_pct"].shift(-horizon)
            f.index.name = "issued_at"
            f = f.reset_index()
            f["point_id"] = str(point_id)
            f["horizon_h"] = horizon
            parts.append(f)
    return pd.concat(parts, ignore_index=True)


def rain_labels(observations: pd.DataFrame, horizons: Sequence[int]) -> pd.DataFrame:
    """1.0 se chover ao menos o limiar na hora que termina em t+h; NaN sem medição."""
    parts = []
    for point_id, group in observations.groupby("point_id", sort=True):
        rain = _regular(group, "observed_at")["precipitation_mm"]
        for horizon in horizons:
            future = rain.shift(-horizon)
            label = (future >= RAIN_THRESHOLD_MM).astype(float).where(future.notna())
            f = pd.DataFrame({LABEL: label})
            f.index.name = "issued_at"
            f = f.reset_index()
            f["point_id"] = str(point_id)
            f["horizon_h"] = horizon
            parts.append(f)
    return pd.concat(parts, ignore_index=True)


def build_dataset(
    observations: pd.DataFrame,
    forecasts: pd.DataFrame | None,
    horizons: Sequence[int] = HORIZONS,
    with_labels: bool = True,
) -> pd.DataFrame:
    """Uma linha por ponto, instante de emissão e horizonte."""
    base = observation_features(observations)
    grid = pd.DataFrame({"horizon_h": list(horizons)})
    rows = base.merge(grid, how="cross")
    rows = pd.concat([rows, calendar_features(rows["issued_at"])], axis=1)

    keys = list(KEY_COLUMNS)
    if forecasts is not None and not forecasts.empty:
        rows = rows.merge(forecast_features(forecasts, horizons), on=keys, how="left")
    else:
        for column in FORECAST_FEATURES:
            rows[column] = np.nan

    if with_labels:
        rows = rows.merge(rain_labels(observations, horizons), on=keys, how="left")
        rows = rows[rows[LABEL].notna()]
    return rows.sort_values(keys).reset_index(drop=True)
