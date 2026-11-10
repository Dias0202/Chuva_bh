"""Cliente das APIs de previsão da Open-Meteo (dados CC BY 4.0, uso não comercial)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import urlencode

from ingestion.columns import Row
from ingestion.http import Fetch
from ingestion.points import Point

FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"
HISTORICAL_URL: Final = "https://historical-forecast-api.open-meteo.com/v1/forecast"

SOURCE_FORECAST: Final = "open_meteo_forecast"
SOURCE_HISTORICAL: Final = "open_meteo_historical_forecast"

HOURLY_COLUMNS: Final[Mapping[str, str]] = {
    "precipitation": "precipitation_mm",
    "precipitation_probability": "precipitation_probability_pct",
    "temperature_2m": "temperature_c",
    "relative_humidity_2m": "relative_humidity_pct",
    "dew_point_2m": "dew_point_c",
    "surface_pressure": "pressure_hpa",
    "cape": "cape_j_kg",
    "cloud_cover": "cloud_cover_pct",
    "wind_direction_10m": "wind_direction_deg",
    "wind_gusts_10m": "wind_gust_ms",
    "wind_speed_10m": "wind_speed_ms",
}


class OpenMeteoFormatError(ValueError):
    """A resposta da API não tem o formato esperado."""


def build_url(base_url: str, point: Point, **extra: str | int) -> str:
    params: dict[str, str | int | float] = {
        "latitude": point.latitude,
        "longitude": point.longitude,
        "hourly": ",".join(HOURLY_COLUMNS),
        "timezone": "UTC",
        "wind_speed_unit": "ms",
        **extra,
    }
    return f"{base_url}?{urlencode(params)}"


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OpenMeteoFormatError(f"valor não numérico: {value!r}")
    return float(value)


def parse_hourly(
    body: bytes, point_id: str, source: str, issued_at: datetime | None = None
) -> list[Row]:
    """Converte a resposta em linhas, descartando horários anteriores à rodada."""
    try:
        payload = json.loads(body)
        hourly = payload["hourly"]
        times = hourly["time"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise OpenMeteoFormatError("resposta sem o bloco hourly") from error

    series: dict[str, list[Any]] = {}
    for api_name in HOURLY_COLUMNS:
        values = hourly.get(api_name)
        if not isinstance(values, list) or len(values) != len(times):
            raise OpenMeteoFormatError(f"série ausente ou de tamanho incorreto: {api_name}")
        series[api_name] = values

    rows: list[Row] = []
    for index, raw_time in enumerate(times):
        valid_at = datetime.fromisoformat(raw_time)
        if issued_at is not None and valid_at < issued_at:
            continue
        row: Row = {
            "point_id": point_id,
            "valid_at": valid_at,
            "issued_at": issued_at,
            "source": source,
        }
        for api_name, column in HOURLY_COLUMNS.items():
            row[column] = _number(series[api_name][index])
        rows.append(row)
    return rows


def fetch_forecast(point: Point, fetch: Fetch, issued_at: datetime) -> tuple[bytes, list[Row]]:
    """Retorna o JSON bruto e as linhas da previsão operacional atual."""
    body = fetch(build_url(FORECAST_URL, point, forecast_days=3))
    return body, parse_hourly(body, point.point_id, SOURCE_FORECAST, issued_at)


def fetch_historical(point: Point, fetch: Fetch, start: date, end: date) -> list[Row]:
    """Retorna a série histórica da previsão operacional no intervalo."""
    url = build_url(HISTORICAL_URL, point, start_date=start.isoformat(), end_date=end.isoformat())
    return parse_hourly(fetch(url), point.point_id, SOURCE_HISTORICAL)
