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

from features.constants import HORIZONS as HORIZONS
from features.constants import RAIN_THRESHOLD_MM as RAIN_THRESHOLD_MM

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

# Do METAR do aeroporto: sem chuva em mm, só se chove ou troveja. Madrugada sem relatório
# fica como NaN, tanto no treino quanto no scoring.
METAR_FEATURES = (
    "metar_available",
    "metar_temperature_c",
    "metar_dew_depression_c",
    "metar_relative_humidity_pct",
    "metar_pressure_hpa",
    "metar_pressure_tendency_3h_hpa",
    "metar_wind_speed_ms",
    "metar_wind_gust_ms",
    "metar_wind_dir_sin",
    "metar_wind_dir_cos",
    "metar_ceiling_ft",
    "metar_rain_now",
    "metar_thunder_now",
    "metar_rain_hours_3h",
    "metar_rain_hours_6h",
)

FEATURE_SETS = {
    "full": ("horizon_h", *OBSERVATION_FEATURES, *CALENDAR_FEATURES, *FORECAST_FEATURES),
    "obs_only": ("horizon_h", *OBSERVATION_FEATURES, *CALENDAR_FEATURES),
    "metar": ("horizon_h", *METAR_FEATURES, *CALENDAR_FEATURES, *FORECAST_FEATURES),
}


def _regular(group: pd.DataFrame, time_column: str) -> pd.DataFrame:
    """Indexa por hora, sem lacunas no índice: horas ausentes viram NaN."""
    frame = group.sort_values(time_column).drop_duplicates(time_column, keep="last")
    # Objeto de offset em vez de alias em texto: o alias de hora mudou entre versões do pandas.
    return frame.set_index(time_column).asfreq(pd.offsets.Hour())


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


def relative_humidity(temperature: pd.Series, dew_point: pd.Series) -> pd.Series:
    """Umidade relativa (%) pela fórmula de Magnus."""
    a, b = 17.625, 243.04
    return (
        100 * np.exp(a * dew_point / (b + dew_point)) / np.exp(a * temperature / (b + temperature))
    )


def metar_features(metar: pd.DataFrame, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Features do METAR por hora. `end` estende a grade até a hora pedida no scoring."""
    parts = []
    for point_id, group in metar.groupby("point_id", sort=True):
        g = _regular(group, "observed_at")
        if end is not None and end > g.index.max():
            g = g.reindex(pd.date_range(g.index.min(), end, freq=pd.offsets.Hour()))
        reported = g["source"].notna()
        temperature = g["temperature_c"]
        pressure = g["pressure_hpa"]
        rain = g["rain"]
        direction = np.deg2rad(g["wind_direction_deg"])

        f = pd.DataFrame(index=g.index)
        f["metar_available"] = reported.astype(float)
        f["metar_temperature_c"] = temperature
        f["metar_dew_depression_c"] = temperature - g["dew_point_c"]
        f["metar_relative_humidity_pct"] = relative_humidity(temperature, g["dew_point_c"])
        f["metar_pressure_hpa"] = pressure
        f["metar_pressure_tendency_3h_hpa"] = pressure - pressure.shift(3)
        f["metar_wind_speed_ms"] = g["wind_speed_ms"]
        f["metar_wind_gust_ms"] = g["wind_gust_ms"]
        f["metar_wind_dir_sin"] = np.sin(direction)
        f["metar_wind_dir_cos"] = np.cos(direction)
        # Sem camada de teto num relatório existente é céu aberto, não dado ausente.
        f["metar_ceiling_ft"] = g["ceiling_ft"].where(~reported | g["ceiling_ft"].notna(), 30000.0)
        f["metar_rain_now"] = rain
        f["metar_thunder_now"] = g["thunder"]
        f["metar_rain_hours_3h"] = rain.rolling(3, min_periods=1).sum()
        f["metar_rain_hours_6h"] = rain.rolling(6, min_periods=1).sum()
        f.index.name = "issued_at"
        f = f.reset_index()
        f["point_id"] = str(point_id)
        parts.append(f)
    return _with_ns(pd.concat(parts, ignore_index=True))


def _with_ns(frame: pd.DataFrame) -> pd.DataFrame:
    """Mesma resolução de horário em todas as tabelas, para o merge não falhar."""
    frame["issued_at"] = pd.to_datetime(frame["issued_at"]).astype("datetime64[ns]")
    return frame


def build_dataset(
    observations: pd.DataFrame | None,
    forecasts: pd.DataFrame | None,
    horizons: Sequence[int] = HORIZONS,
    with_labels: bool = True,
    metar: pd.DataFrame | None = None,
    issued: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Uma linha por ponto, instante de emissão e horizonte.

    Sem observações do INMET (modo METAR no scoring), `issued` define os pontos e as horas.
    """
    if observations is not None and not observations.empty:
        base = _with_ns(observation_features(observations))
    elif issued is not None and not with_labels:
        base = _with_ns(issued[["point_id", "issued_at"]].copy())
        for column in OBSERVATION_FEATURES:
            base[column] = np.nan
    else:
        raise ValueError("sem observações: informe `issued` e use with_labels=False")
    # Produto cartesiano por chave constante: `how="cross"` não existe no pandas antigo
    # dos containers do SageMaker.
    grid = pd.DataFrame({"horizon_h": list(horizons), "_join": 0})
    rows = base.assign(_join=0).merge(grid, on="_join").drop(columns="_join")
    rows = pd.concat([rows, calendar_features(rows["issued_at"])], axis=1)

    keys = list(KEY_COLUMNS)
    if forecasts is not None and not forecasts.empty:
        rows = rows.merge(_with_ns(forecast_features(forecasts, horizons)), on=keys, how="left")
    else:
        for column in FORECAST_FEATURES:
            rows[column] = np.nan

    if metar is not None and not metar.empty:
        rows = rows.merge(
            metar_features(metar, rows["issued_at"].max()),
            on=["point_id", "issued_at"],
            how="left",
        )
    for column in METAR_FEATURES:
        if column not in rows.columns:
            rows[column] = np.nan
    rows["metar_available"] = rows["metar_available"].fillna(0.0)

    if with_labels:
        assert observations is not None  # noqa: S101
        rows = rows.merge(_with_ns(rain_labels(observations, horizons)), on=keys, how="left")
        rows = rows[rows[LABEL].notna()]
    return rows.sort_values(keys).reset_index(drop=True)
