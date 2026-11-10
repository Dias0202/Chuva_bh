"""Definição dos datasets, compartilhada entre validação, Parquet e catálogo Glue."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

Row = dict[str, datetime | float | str | None]

TIMESTAMP: Final = "timestamp"
DOUBLE: Final = "double"
STRING: Final = "string"


@dataclass(frozen=True)
class Column:
    name: str
    glue_type: str
    min_value: float | None = None
    max_value: float | None = None


@dataclass(frozen=True)
class Dataset:
    name: str
    columns: tuple[Column, ...]
    key_columns: tuple[str, ...]
    time_column: str


# Faixas físicas plausíveis para o Sudeste do Brasil. Um valor fora delas é erro
# de sensor ou de modelo, não um evento extremo.
_PRECIPITATION = (0.0, 200.0)
_PRESSURE = (800.0, 1100.0)
_TEMPERATURE = (-10.0, 50.0)
_DEW_POINT = (-40.0, 40.0)
_PERCENT = (0.0, 100.0)
_DIRECTION = (0.0, 360.0)
_WIND = (0.0, 80.0)

OBSERVATIONS: Final = Dataset(
    name="observations",
    columns=(
        Column("observed_at", TIMESTAMP),
        Column("source", STRING),
        Column("precipitation_mm", DOUBLE, *_PRECIPITATION),
        Column("pressure_hpa", DOUBLE, *_PRESSURE),
        Column("radiation_kj_m2", DOUBLE, 0.0, 6000.0),
        Column("temperature_c", DOUBLE, *_TEMPERATURE),
        Column("dew_point_c", DOUBLE, *_DEW_POINT),
        Column("relative_humidity_pct", DOUBLE, *_PERCENT),
        Column("wind_direction_deg", DOUBLE, *_DIRECTION),
        Column("wind_gust_ms", DOUBLE, *_WIND),
        Column("wind_speed_ms", DOUBLE, *_WIND),
    ),
    key_columns=("point_id", "observed_at"),
    time_column="observed_at",
)

FORECASTS: Final = Dataset(
    name="forecasts",
    columns=(
        Column("valid_at", TIMESTAMP),
        # Nulo no histórico: a Historical Forecast API não informa a rodada de origem.
        Column("issued_at", TIMESTAMP),
        Column("source", STRING),
        Column("precipitation_mm", DOUBLE, *_PRECIPITATION),
        Column("precipitation_probability_pct", DOUBLE, *_PERCENT),
        Column("temperature_c", DOUBLE, *_TEMPERATURE),
        Column("relative_humidity_pct", DOUBLE, *_PERCENT),
        Column("dew_point_c", DOUBLE, *_DEW_POINT),
        Column("pressure_hpa", DOUBLE, *_PRESSURE),
        Column("cape_j_kg", DOUBLE, 0.0, 10000.0),
        Column("cloud_cover_pct", DOUBLE, *_PERCENT),
        Column("wind_direction_deg", DOUBLE, *_DIRECTION),
        Column("wind_gust_ms", DOUBLE, *_WIND),
        Column("wind_speed_ms", DOUBLE, *_WIND),
    ),
    key_columns=("point_id", "issued_at", "valid_at"),
    time_column="valid_at",
)
