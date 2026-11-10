"""Cliente da API de estações do INMET, que exige chave de acesso.

O formato da resposta segue o da API pública antiga do INMET e ainda não foi
conferido com uma chave válida. Qualquer campo inesperado gera erro explícito.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Final

from ingestion.columns import Row
from ingestion.http import Fetch

URL_TEMPLATE: Final = "https://apitempo.inmet.gov.br/token/estacao/{start}/{end}/{code}/{token}"
SOURCE_INMET_API: Final = "inmet_api"

FIELDS: Final[Mapping[str, str]] = {
    "CHUVA": "precipitation_mm",
    "PRE_INS": "pressure_hpa",
    "RAD_GLO": "radiation_kj_m2",
    "TEM_INS": "temperature_c",
    "PTO_INS": "dew_point_c",
    "UMD_INS": "relative_humidity_pct",
    "VEN_DIR": "wind_direction_deg",
    "VEN_RAJ": "wind_gust_ms",
    "VEN_VEL": "wind_speed_ms",
}


class InmetApiError(ValueError):
    """A resposta da API não tem o formato esperado ou recusou a chave."""


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError as error:
        raise InmetApiError(f"valor não numérico: {value!r}") from error


def parse_response(body: bytes, point_id: str) -> list[Row]:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    if not text.startswith("["):
        # A API responde texto puro quando a chave é inválida.
        raise InmetApiError(f"resposta inesperada da API: {text[:40]!r}")
    try:
        records = json.loads(text)
    except json.JSONDecodeError as error:
        raise InmetApiError("JSON inválido") from error

    rows: list[Row] = []
    for record in records:
        try:
            observed_at = datetime.strptime(
                f"{record['DT_MEDICAO']} {record['HR_MEDICAO']}", "%Y-%m-%d %H%M"
            )
        except (KeyError, TypeError, ValueError) as error:
            raise InmetApiError(f"registro sem data e hora válidas: {record!r}"[:120]) from error
        row: Row = {"point_id": point_id, "observed_at": observed_at, "source": SOURCE_INMET_API}
        for field, column in FIELDS.items():
            row[column] = _number(record.get(field))
        rows.append(row)
    return rows


def fetch_recent(point_id: str, token: str, fetch: Fetch, start: date, end: date) -> list[Row]:
    url = URL_TEMPLATE.format(
        start=start.isoformat(), end=end.isoformat(), code=point_id, token=token
    )
    return parse_response(fetch(url), point_id)
